"""Orquestación del dispositivo: inicialización, estados y backend
(HU-DEVICE-002 + HU-DEVICE-003 buffer offline/sync/evidencia).

Flujo de arranque (AC-001, presupuesto < 60s RNF-1.1):
  sonido -> cámara (AS-08 ok / AS-09 error) -> modelo visión (AS-09 degradado
  si falla, sin abortar) -> presencia -> self-register (AC-007, acotado) ->
  pull device_config (AC-005, best-effort) -> chequeo inicial rostro/FOV ->
  running.

Estados locales: REGISTRADO -> ASIGNADO -> ACTIVO <-> ESPERA.
ESPERA = sub-estado local de presencia (sin rostro > 30s, AC-003) u
obstrucción > 30s con pausa de detección (AC-002). Frente al backend el
device en ESPERA sigue siendo ACTIVE (ver models.LOCAL_STATUS_TO_BACKEND).
OFFLINE = sin conectividad (AC-004): se pausa la detección y el heartbeat
restaura ACTIVO/ESPERA al reconectar. SUSPENDIDO/RETIRADO los impone el
backend vía heartbeat y pausan la detección hasta intervención admin.
"""
from __future__ import annotations
import asyncio
import json
import logging
import shutil
import signal
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from app.common.models import (
    BACKEND_STATUS_TO_LOCAL,
    DeviceConfig,
    DeviceIdentity,
    DeviceState,
    AlertCode,
    HeartbeatPayload,
)
from app.common.config import (
    apply_remote_config,
    load_cached_remote,
    load_default_config,
    load_env_config,
    load_local_override,
    get_sound_pattern,
)
from app.device.backend import (
    BackendAuthError,
    BackendClient,
    BackendConflictError,
    BackendError,
)
from app.device.identity import (
    data_dir,
    load_or_create_identity,
    save_credentials,
    save_identity,
    stable_idempotency_key,
    update_device_id,
    update_state,
)
from app.capture.camera import Camera, CameraError
from app.analysis.detector import Detector, DetectionResult
from app.monitoring.presence import PresenceMonitor
from app.alerting.sound_player import SoundPlayer
from app.common.models import build_telemetry_event, new_event_id

try:
    from app.analysis.pipeline import VisionPipeline
    HAS_PIPELINE = True
except Exception:  # pragma: no cover - import ligero siempre disponible
    VisionPipeline = None  # type: ignore
    HAS_PIPELINE = False


logger = logging.getLogger(__name__)

# Presupuesto de arranque RNF-1.1 (AC-001) y cotas para no bloquearlo.
BOOT_BUDGET_SEC = 60.0
SELF_REGISTER_BOOT_ATTEMPTS = 2
SELF_REGISTER_RETRY_DELAY_SEC = 2.0
SELF_REGISTER_BG_RETRY_SEC = 60.0
INITIAL_FACE_CHECK_FRAMES = 3
DETECTOR_RETRY_SEC = 30.0
# HU-DEVICE-003: almacenamiento crítico y retención.
STORAGE_CRIT_USED_PCT = 90
STORAGE_ALERT_COOLDOWN_SEC = 86400.0  # AS-09 por disco lleno: máx 1/día
RETENTION_CHECK_SEC = 86400.0  # job purga FAILED/evidencia 7d: 1/día


@dataclass
class DeviceContext:
    identity: DeviceIdentity
    config: DeviceConfig
    env_config: dict
    camera: Optional[Camera] = None
    detector: Optional[Detector] = None
    pipeline: Optional[object] = None  # VisionPipeline HU-DEVICE-001 (None si no disponible)
    presence: Optional[PresenceMonitor] = None
    sound_player: Optional[SoundPlayer] = None
    backend: Optional[BackendClient] = None
    running: bool = False
    current_state: DeviceState = DeviceState.REGISTRADO
    backend_available: bool = False
    detect_available: bool = False
    boot_start_monotonic: float = field(default_factory=time.monotonic)
    pre_offline_state: DeviceState = DeviceState.ACTIVO
    last_register_attempt: float = 0.0
    last_detector_retry: float = 0.0
    last_config_pull: float = 0.0
    # HU-DEVICE-003: buffer/sync (se crean en __init__; None en tests legacy).
    buffer: Optional[object] = None
    sync_engine: Optional[object] = None
    connectivity: Optional[object] = None
    last_retention_monotonic: float = 0.0
    last_storage_alert_monotonic: float = 0.0


class DeviceManager:
    def __init__(self):
        identity = load_or_create_identity()
        env_config = load_env_config()
        # Orden: default -> caché del último pull manual -> override local.
        # La caché evita que un reinicio revierta a default mientras la API
        # reporta applied==global (sin pending) y el device quedaría obsoleto.
        # El override sigue ganando (ajuste en campo).
        config = load_local_override(
            load_cached_remote(load_default_config(), data_dir()), data_dir())
        try:
            cache_path = data_dir() / "device_config.cache.json"
            if cache_path.exists():
                logger.info("Configuración restaurada desde caché: %s", cache_path)
        except OSError:
            pass
        self.ctx = DeviceContext(
            identity=identity,
            config=config,
            env_config=env_config,
            backend=BackendClient(
                env_config.get("api_url", "http://localhost:8000"),
                timeout_sec=float(env_config.get("backend_timeout_sec", 10.0)),
            ),
            boot_start_monotonic=time.monotonic(),
        )
        self._init_edge_sync()
        self._shutdown = asyncio.Event()
        self._tasks: list[asyncio.Task] = []

    def _init_edge_sync(self) -> None:
        """Crea buffer SQLite + motor sync + monitor conectividad (HU-DEVICE-003).

        Best-effort: si el disco falla, el device sigue operando sin buffer
        (los eventos solo se loguean) en vez de abortar el arranque.
        """
        try:
            from app.storage.buffer import EventBuffer
            from app.sync.connectivity import ConnectivityMonitor
            from app.sync.engine import SyncEngine
            db_path = data_dir() / "db" / "somnguard_local.db"
            self.ctx.buffer = EventBuffer(db_path)
            self.ctx.sync_engine = SyncEngine(
                self.ctx.buffer, self.ctx.backend,
                batch_limit=100,
                data_dir=data_dir(),
            )
            self.ctx.connectivity = ConnectivityMonitor(
                self.ctx.backend,
                interval_sec=int(getattr(self.ctx.config, "sync_interval_sec", 30) or 30),
                on_online=self._on_reconnected,
            )
            logger.info("Buffer offline listo: %s", db_path)
        except Exception as e:
            logger.error("Buffer offline no disponible (%s): eventos solo en log", e)
            self.ctx.buffer = None
            self.ctx.sync_engine = None
            self.ctx.connectivity = None

    def _on_reconnected(self) -> None:
        """Callback OFFLINE→ONLINE: reset backoff para sync inmediato."""
        try:
            eng = getattr(self.ctx, "sync_engine", None)
            if eng is not None and hasattr(eng, "_register_success"):
                eng._register_success()
        except Exception:
            pass

    # -- arranque ------------------------------------------------------
    async def initialize(self) -> None:
        boot_start = self.ctx.boot_start_monotonic
        identity = self.ctx.identity
        logger.info("Inicializando dispositivo serial=%s firmware=%s",
                    identity.serial_number, identity.firmware_version)

        self.ctx.sound_player = SoundPlayer(self.ctx.config)
        await self._timed("cámara", self._init_camera)
        await self._timed("modelo visión", self._init_detector)
        await self._timed("presencia", self._init_presence)

        await self._timed("registro", self._register_device)
        # Config remota solo manual (pending flag del heartbeat), no en arranque

        await self._timed("chequeo inicial", self._initial_face_check)

        self.ctx.running = True
        total = time.monotonic() - boot_start
        logger.info("Dispositivo inicializado en %.1fs (estado=%s, backend=%s)",
                    total, self.ctx.current_state.value,
                    "conectado" if self.ctx.backend_available else "local")
        if total > BOOT_BUDGET_SEC:
            logger.warning("Arranque superó el presupuesto de %.0fs (RNF-1.1): %.1fs",
                           BOOT_BUDGET_SEC, total)

    async def _timed(self, phase: str, coro, *args) -> None:
        start = time.monotonic()
        await coro(*args)
        logger.debug("Fase '%s' completada en %.2fs", phase, time.monotonic() - start)

    async def _init_camera(self) -> None:
        try:
            self.ctx.camera = Camera(
                resolution=self.ctx.config.camera_resolution,
                fps=self.ctx.config.camera_fps,
            )
            await self.ctx.camera.start()
            await self._play_startup_alert()
            logger.info("Cámara inicializada: %s @%sfps",
                        self.ctx.config.camera_resolution, self.ctx.config.camera_fps)
        except CameraError as e:
            logger.error("Error inicializando cámara: %s", e)
            await self._play_error_alert()
            self.ctx.current_state = DeviceState.ERROR
            try:
                save_identity(update_state(self.ctx.identity, DeviceState.ERROR))
            except OSError:
                pass
            raise

    async def _play_startup_alert(self) -> None:
        pattern = get_sound_pattern(self.ctx.config, AlertCode.AS_08)
        await self.ctx.sound_player.play(pattern)

    async def _play_error_alert(self) -> None:
        pattern = get_sound_pattern(self.ctx.config, AlertCode.AS_09)
        await self.ctx.sound_player.play(pattern)

    async def _init_detector(self) -> None:
        try:
            self.ctx.detector = Detector(self.ctx.config.detection_thresholds)
            await self.ctx.detector.load_model()
            self.ctx.detect_available = True
            logger.info("Detector cargado")
        except Exception as e:
            # Degradado: sin modelo no hay detección; se opera en ESPERA y se
            # reintenta en el loop de captura (AC-001 fallback + AS-09).
            self.ctx.detect_available = False
            self.ctx.last_detector_retry = time.monotonic()
            logger.error("No se pudo cargar el modelo de visión (%s); modo degradado", e)
            await self._play_error_alert()
            return
        # HU-DEVICE-001: pipeline compone obstruction + somnolencia +
        # distracción + cinturón sobre el mismo Detector (no duplica modelos).
        if HAS_PIPELINE:
            try:
                pipe = VisionPipeline(self.ctx.config.detection_thresholds)
                # Reusa el landmarker ya cargado (un solo FaceLandmarker).
                pipe.obstruction = self.ctx.detector
                pipe.update_thresholds(self.ctx.config.detection_thresholds)
                await pipe.load_models()
                self.ctx.pipeline = pipe
                logger.info("Pipeline visión HU-DEVICE-001 activo: %s", pipe.models_status)
            except Exception as e:
                logger.warning("Pipeline no disponible, solo obstrucción: %s", e)
                self.ctx.pipeline = None

    async def _init_presence(self) -> None:
        timeout = self.ctx.config.detection_thresholds.get("face_absence_timeout_sec", 30)
        try:
            timeout = int(timeout)
        except (TypeError, ValueError):
            timeout = 30
        self.ctx.presence = PresenceMonitor(face_absence_timeout_sec=timeout)
        self.ctx.presence.on_state_change = self._on_presence_change
        logger.info("Monitor de presencia iniciado (timeout=%ss)", timeout)

    def _on_presence_change(self, face_present: bool) -> None:
        # Solo gobierna el par ACTIVO <-> ESPERA; nunca sale de OFFLINE ni de
        # estados administrativos (AC-003/AC-004).
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        if face_present:
            if self.ctx.current_state == DeviceState.ESPERA:
                loop.create_task(self._transition_to(DeviceState.ACTIVO))
        else:
            if self.ctx.current_state == DeviceState.ACTIVO:
                loop.create_task(self._transition_to(DeviceState.ESPERA))

    async def _initial_face_check(self) -> None:
        """Chequeo inicial multi-frame de rostro y campo visual (AC-002/AC-003).

        Corrección 2026-09-12 v2: si la cámara está tapada/obstruida al arrancar
        NO suena inmediato; se inicia el timer de obstrucción para que el
        escalamiento sea idéntico al runtime: AS-09 a 5s, AS-09 a 20s, pausa a 30s
        (y AS-09 una vez al pausar). Error de HW (sin cámara/detector) sí suena
        inmediato.
        """
        if not self.ctx.camera or not self.ctx.detector or not self.ctx.detect_available:
            logger.info("Chequeo inicial omitido (cámara/detector no disponible); estado ESPERA")
            self.ctx.presence.mark_absent()
            await self._play_error_alert()
            await self._transition_to(DeviceState.ESPERA)
            return
        faces = 0
        fov_bad = 0
        tried = 0
        for _ in range(INITIAL_FACE_CHECK_FRAMES):
            frame = await self.ctx.camera.read_frame()
            if frame is None:
                continue
            tried += 1
            face_present = await self.ctx.detector.detect_face_only(frame)
            faces += 1 if face_present else 0
            if face_present:
                result = await self.ctx.detector.process(frame)
                if not result.fov_ok:
                    fov_bad += 1
        if tried == 0:
            logger.warning("Chequeo inicial sin frames (cámara tapada/desconectada); inicia timer 5/20/30 — permanece ACTIVO para escalamiento")
            self.ctx.presence.mark_absent()
            if self.ctx.detector and self.ctx.detector._obstruction_start_time is None:
                self.ctx.detector._obstruction_start_time = time.monotonic()
                self.ctx.detector._alert_stage = 0
            await self._transition_to(DeviceState.ACTIVO)
            return
        if faces > 0:
            if fov_bad:
                logger.warning("Campo visual deficiente en arranque (mala posición/obstrucción %d/%d); "
                               "se inicia en ACTIVO — escalamiento 5/20/30 activo", fov_bad, tried)
                if self.ctx.detector and self.ctx.detector._obstruction_start_time is None:
                    self.ctx.detector._obstruction_start_time = time.monotonic()
                    self.ctx.detector._alert_stage = 0
            self.ctx.presence.mark_present()
            await self._transition_to(DeviceState.ACTIVO)
        else:
            logger.warning("Campo visual obstruido al arrancar (sin rostro %d/%d frames) — inicia timer 5/20/30, permanece ACTIVO", tried, INITIAL_FACE_CHECK_FRAMES)
            self.ctx.presence.mark_absent()
            if self.ctx.detector and self.ctx.detector._obstruction_start_time is None:
                self.ctx.detector._obstruction_start_time = time.monotonic()
                self.ctx.detector._alert_stage = 0
            await self._transition_to(DeviceState.ACTIVO)

    # -- registro / provisioning (AC-007) --------------------------------
    async def _register_device(self) -> None:
        if self.ctx.identity.has_credentials():
            logger.info("Dispositivo ya registrado: %s", self.ctx.identity.device_id)
            self.ctx.backend_available = True
            # No ASIGNADO local: el device no sabe si está asignado (solo backend)
            # Queda REGISTRADO hasta _initial_face_check decida ACTIVO/ESPERA
            return

        provision_token = (self.ctx.env_config.get("provision_token", "") or "").strip()
        if not provision_token:
            await self._enter_local_mode("sin credenciales ni provision token")
            return

        self.ctx.last_register_attempt = time.monotonic()
        for attempt in range(1, SELF_REGISTER_BOOT_ATTEMPTS + 1):
            try:
                created, body = await self.ctx.backend.self_register(
                    self.ctx.identity.serial_number,
                    self.ctx.identity.firmware_version,
                    provision_token,
                    stable_idempotency_key(self.ctx.identity.serial_number),
                )
                if await self._handle_self_register_result(created, body):
                    return
                return  # 200 sin secretos y sin key local: no reintentar igual
            except BackendAuthError as e:
                logger.warning("Self-register rechazado (token inválido/expirado): %s", e)
                await self._enter_local_mode("token rechazado por el backend")
                return
            except BackendConflictError as e:
                logger.error("Self-register en conflicto (serial duplicado): %s", e)
                await self._enter_local_mode("serial en conflicto")
                return
            except BackendError as e:
                logger.warning("Self-register intento %d/%d fallido: %s",
                               attempt, SELF_REGISTER_BOOT_ATTEMPTS, e)
                if attempt < SELF_REGISTER_BOOT_ATTEMPTS:
                    await asyncio.sleep(SELF_REGISTER_RETRY_DELAY_SEC)
        # Sin red: modo local autónomo; el heartbeat reintentará en background.
        await self._enter_local_mode("backend no alcanzable (reintento en background)")

    async def _handle_self_register_result(self, created: bool, body: dict) -> bool:
        """Procesa 201/200. Retorna True si el registro quedó completo."""
        parsed = self.ctx.backend.parse_self_register(body, created)
        device_id = parsed["device_id"]
        if not device_id:
            logger.error("Self-register sin device_id en respuesta; modo local")
            await self._enter_local_mode("respuesta inválida del backend")
            return False
        if created and parsed["api_key"]:
            # 201: persiste device_id + api_key seguro y deja de usar el token.
            self.ctx.identity = save_credentials(self.ctx.identity, device_id, parsed["api_key"])
            self.ctx.backend_available = True
            # Sin ASIGNADO local — queda REGISTRADO hasta face_check
            logger.info("Auto-registro completado: device_id=%s estado=%s",
                        device_id, parsed["status"])
            return True
        # 200 = reintento idempotente sin reexposición de secretos (ADR-010).
        if self.ctx.identity.has_credentials():
            logger.info("Self-register idempotente (200): credenciales locales vigentes, device_id=%s",
                        self.ctx.identity.device_id)
            self.ctx.backend_available = True
            return True
        logger.error("Self-register idempotente (200) sin api_key local: el reintento no "
                     "reexpone la key (ADR-010); se requiere rotación administrativa "
                     "(PATCH /devices/{id}/rotate-key). Modo local.")
        await self._enter_local_mode("key no recuperable vía reintento")
        return False

    async def _enter_local_mode(self, reason: str) -> None:
        if not self.ctx.identity.device_id:
            self.ctx.identity = update_device_id(
                self.ctx.identity, f"local-{self.ctx.identity.serial_number}")
        self.ctx.backend_available = False
        logger.info("Dispositivo en modo local (%s): %s", reason, self.ctx.identity.device_id)
        # Sin ASIGNADO local

    # -- config remota (AC-005/HU-API-005 manual via pending flag del heartbeat) --
    # Flujo: PATCH /catalogs/sound-patterns -> global_config.version++ ;
    # portal muestra outdated vía GET /config/status ; usuario pulsa Actualizar
    # (POST /devices/{id}/config/refresh -> pending=true) ; el próximo heartbeat
    # trae configPending=true y aquí se hace GET /config (persiste applied en API).
    async def _pull_remote_config(self, reason: str) -> bool:
        if not self.ctx.identity.has_credentials():
            logger.warning("GET config omitido (%s): sin credenciales (device_id=%s)",
                           reason, self.ctx.identity.device_id)
            return False
        try:
            remote = await self.ctx.backend.fetch_config(
                self.ctx.identity.device_id, self.ctx.identity.api_key)
        except BackendAuthError as e:
            logger.warning("GET config rechazado (%s): key inválida o device suspendido/retirado: %s",
                           reason, e)
            return False
        except BackendError as e:
            # 500 aquí suele ser la API (ver DeviceConfigService.getEffectiveConfig);
            # antes era silencioso (debug) y parecía que "solo dice pendiente".
            logger.warning("GET config fallido (%s): %s", reason, e)
            return False
        if not remote:
            logger.warning("GET config vacío (%s): backend devolvió 404/None; se usa config local",
                           reason)
            return False
        merged, changed = apply_remote_config(self.ctx.config, remote)
        if changed:
            self.ctx.config = merged
            await self._reconfigure_services()
            self._cache_remote_config(remote)
            logger.info("device_config aplicada (%s): umbrales=%d sound_patterns=%d "
                        "volumen=%.2f heartbeat=%ss sync=%ss",
                        reason, len(merged.detection_thresholds),
                        len(merged.sound_patterns), merged.volume_scale,
                        merged.heartbeat_interval_sec, merged.sync_interval_sec)
        else:
            logger.info("GET config sin cambios aplicables (%s): remote_keys=%s "
                        "(thresholds/event_sound_map del catálogo se ignoran; "
                        "solo aplican %s)",
                        reason, sorted(remote.keys()) if isinstance(remote, dict) else type(remote).__name__,
                        "detection_thresholds/sound_patterns/volumen/intervalos")
        self.ctx.last_config_pull = time.monotonic()
        return changed

    def _cache_remote_config(self, remote: dict) -> None:
        try:
            cache_path = data_dir() / "device_config.cache.json"
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            with open(cache_path, "w", encoding="utf-8") as f:
                json.dump({
                    "pulled_at": datetime.now(timezone.utc).isoformat(),
                    "config": remote,
                }, f, indent=2)
        except OSError as e:
            logger.debug("No se pudo cachear device_config: %s", e)

    async def _reconfigure_services(self) -> None:
        if self.ctx.detector:
            self.ctx.detector.update_thresholds(self.ctx.config.detection_thresholds)
        if getattr(self.ctx, "pipeline", None):
            try:
                self.ctx.pipeline.update_thresholds(self.ctx.config.detection_thresholds)
            except Exception:
                pass
        if self.ctx.sound_player:
            self.ctx.sound_player.update_config(self.ctx.config)
        if self.ctx.camera and self.ctx.config.camera_resolution:
            await self.ctx.camera.set_resolution(self.ctx.config.camera_resolution)
        timeout = self.ctx.config.detection_thresholds.get("face_absence_timeout_sec")
        if timeout and self.ctx.presence:
            try:
                self.ctx.presence.timeout_sec = float(timeout)
            except (TypeError, ValueError):
                pass

    # -- transiciones ------------------------------------------------------
    async def _transition_to(self, new_state: DeviceState) -> None:
        if self.ctx.current_state == new_state:
            return
        logger.info("Cambio de estado: %s -> %s", self.ctx.current_state.value, new_state.value)
        self.ctx.current_state = new_state
        # Al volver a ACTIVO el timer de obstrucción 5/20/30 quedó congelado
        # durante ESPERA (ahí no corre detector.process, solo detect_face_only).
        # Sin reset, el primer frame regular con un parpadeo de FOV re-pausa
        # instantáneo con un elapsed de 60-90s (flap ESPERA->ACTIVO->ESPERA).
        # Ventana fresca al re-activar.
        if new_state == DeviceState.ACTIVO:
            try:
                det = getattr(self.ctx, "detector", None)
                if det is not None and hasattr(det, "reset_obstruction"):
                    det.reset_obstruction()
                pipe = getattr(self.ctx, "pipeline", None)
                obs = getattr(pipe, "obstruction", None) if pipe is not None else None
                if obs is not None and obs is not det and hasattr(obs, "reset_obstruction"):
                    obs.reset_obstruction()
            except Exception as e:
                logger.debug("No se pudo resetear timer obstrucción: %s", e)
        try:
            self.ctx.identity = update_state(self.ctx.identity, new_state)
        except OSError as e:
            logger.debug("No se pudo persistir estado: %s", e)

    async def _enter_offline(self, reason: str) -> None:
        # Corrección 2026-09-12 (offline-first): OFFLINE ya NO pausa la detección.
        # Solo cambia el modo (backend_available=False) y mantiene el monitoreo local
        # con buffer offline. La captura continúa en ACTIVO/OFFLINE.
        self.ctx.backend_available = False
        if self.ctx.current_state in (DeviceState.ACTIVO, DeviceState.ESPERA):
            self.ctx.pre_offline_state = self.ctx.current_state
            await self._transition_to(DeviceState.OFFLINE)
            logger.warning("Sin conectividad (%s): modo OFFLINE (detección local continúa, sin backend)", reason)
        elif self.ctx.current_state == DeviceState.OFFLINE:
            logger.debug("Sigue sin conectividad (%s): permanece OFFLINE (monitoreo local activo)", reason)
        else:
            # Si estaba en ASIGNADO/REGISTRADO y falla heartbeat (raro), solo marcar flag
            logger.info("Sin conectividad (%s): permanece %s (backend no disponible, monitoreo sigue)", reason, self.ctx.current_state.value)

    async def _recover_from_offline(self) -> None:
        if self.ctx.current_state != DeviceState.OFFLINE:
            return
        # Recuperación robusta: rostro presente -> ACTIVO, sin rostro -> ESPERA
        # (no usar pre_offline_state para evitar ACTIVO sin rostro)
        target = DeviceState.ACTIVO if self.ctx.presence.face_present else DeviceState.ESPERA
        logger.info("Conectividad restaurada (heartbeat OK): OFFLINE -> %s (face_present=%s, pre=%s)",
                    target.value, self.ctx.presence.face_present,
                    self.ctx.pre_offline_state.value if hasattr(self.ctx.pre_offline_state, 'value') else self.ctx.pre_offline_state)
        await self._transition_to(target)

    def _apply_backend_status(self, status: Optional[str]) -> None:
        """Mapea el estado reportado por el backend al estado local (AC-004)."""
        if not status:
            return
        local = BACKEND_STATUS_TO_LOCAL.get(status.strip().upper())
        if local is None:
            logger.debug("Estado backend desconocido: %s", status)
            return
        if local in (DeviceState.SUSPENDIDO, DeviceState.RETIRADO):
            if self.ctx.current_state != local:
                logger.warning("Backend impuso estado %s: detección pausada", local.value)
                self.ctx.current_state = local
                try:
                    self.ctx.identity = update_state(self.ctx.identity, local)
                except OSError:
                    pass

    # -- loops ---------------------------------------------------------------
    async def run(self) -> None:
        loop = asyncio.get_running_loop()
        try:
            if sys.platform != "win32":
                for sig in (signal.SIGTERM, signal.SIGINT):
                    loop.add_signal_handler(sig, self._shutdown.set)
        except (NotImplementedError, RuntimeError) as e:
            logger.debug("Señales no disponibles en esta plataforma: %s", e)

        self._tasks = [
            asyncio.create_task(self._capture_loop(), name="capture"),
            asyncio.create_task(self._heartbeat_loop(), name="heartbeat"),
            asyncio.create_task(self._sync_loop(), name="sync"),
        ]
        try:
            await self._shutdown.wait()
        except (KeyboardInterrupt, asyncio.CancelledError):
            logger.info("Interrupción recibida")
        await self.shutdown()

    async def _capture_loop(self) -> None:
        frame_interval = 1.0 / max(1, self.ctx.config.camera_fps)
        while self.ctx.running:
            start = time.monotonic()
            try:
                state = self.ctx.current_state
                # SUSPENDIDO/RETIRADO/ERROR sí pausan detección (órdenes administrativas).
                # OFFLINE ya NO pausa — corrección 2026-09-12 (offline-first):
                # sin backend el device sigue monitoreando y hace buffer local.
                if state in (DeviceState.SUSPENDIDO,
                             DeviceState.RETIRADO, DeviceState.ERROR):
                    await asyncio.sleep(1.0)
                    continue
                if not self.ctx.detect_available:
                    await self._maybe_retry_detector()
                    await asyncio.sleep(1.0)
                    continue
                # ACTIVO y OFFLINE comparten detección completa (offline-first).
                if state in (DeviceState.ACTIVO, DeviceState.OFFLINE):
                    frame = await self.ctx.camera.read_frame()
                    if frame is not None:
                        t_cap = time.monotonic()
                        if self.ctx.pipeline is not None:
                            result = await self.ctx.pipeline.process(frame, capture_time=t_cap)
                        else:
                            result = await self.ctx.detector.process(frame)
                        await self._handle_detection_result(result, frame)
                        await self._check_obstruction_pause()
                elif state in (DeviceState.ESPERA, DeviceState.ASIGNADO, DeviceState.REGISTRADO):
                    # Un solo frame por iteración (corrección HU-DEVICE-001:
                    # antes leía 2 frames y duplicaba costo de inferencia).
                    frame = await self.ctx.camera.read_frame()
                    if frame is not None:
                        face_present = await self.ctx.detector.detect_face_only(frame)
                        self.ctx.presence.update(face_present)
                else:
                    await asyncio.sleep(1.0)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error("Error en loop de captura: %s", e)
                await asyncio.sleep(1.0)

            elapsed = time.monotonic() - start
            sleep_time = max(0, frame_interval - elapsed)
            if sleep_time > 0:
                await asyncio.sleep(sleep_time)

    async def _maybe_retry_detector(self) -> None:
        if time.monotonic() - self.ctx.last_detector_retry < DETECTOR_RETRY_SEC:
            return
        self.ctx.last_detector_retry = time.monotonic()
        try:
            if self.ctx.detector is None:
                self.ctx.detector = Detector(self.ctx.config.detection_thresholds)
            await self.ctx.detector.load_model()
            self.ctx.detect_available = True
            logger.info("Detector recuperado tras reintento")
        except Exception as e:
            logger.debug("Reintento de modelo fallido: %s", e)

    async def _check_obstruction_pause(self) -> None:
        """Obstrucción > 30s: AS-09 + pausa de detección (ESPERA) — AC-002.

        Corrección 2026-09-12: en OFFLINE no se transiciona fuera de OFFLINE;
        se alerta una sola vez por episodio (no re-dispara 5/20 hasta que se
        despeje la obstrucción). Deja lógica anterior para ACTIVO.
        """
        detector = self.ctx.detector
        if not detector or not detector.needs_detection_pause:
            return
        if self.ctx.current_state == DeviceState.ACTIVO:
            logger.warning("Campo visual obstruido >30s: pausa de detección (ESPERA)")
            await self._play_error_alert()
            await self._transition_to(DeviceState.ESPERA)
        elif self.ctx.current_state == DeviceState.OFFLINE:
            # Solo una vez por episodio para no loopear 5/20 tras pausar
            if detector._alert_stage == 2:  # 2 = ya avisó a 20s, ahora toca pausa
                logger.warning("Campo visual obstruido >30s en OFFLINE: AS-09 (sigue monitoreo local)")
                await self._play_error_alert()
                detector._alert_stage = 3  # marca pausa notificada, no re-dispara hasta despeje
            # No reset: se mantiene hasta que detector.process vea cara y resetee solo

    async def _handle_detection_result(self, result, frame=None) -> None:
        # Duck-typing: DetectionResult (HU-DEVICE-002) o PipelineResult
        # (HU-DEVICE-001) — ambos exponen .events/.face_present/.fov_ok.
        if hasattr(result, "events"):
            try:
                self.ctx.presence.update(bool(result.face_present and result.fov_ok))
            except Exception:
                pass
            events = list(result.events or [])
            metrics = getattr(result, "metrics", None)
            if isinstance(metrics, dict) and metrics.get("within_budget") is False:
                logger.warning("Frame excedió presupuesto RNF-1.2: %s", metrics)
        elif isinstance(result, dict):
            events = result.get("events", [])
        else:
            events = []

        if not events:
            return

        # HU-DEVICE-003: buffer offline SIEMPRE (antes del árbitro de sonido,
        # que puede hacer `return` temprano y saltaría el enqueue).
        try:
            await self._enqueue_events(events, frame)
        except Exception as e:
            logger.debug("Enqueue buffer fallido (evento solo en log): %s", e)

        # Todos los eventos se loguean; pero suena UNO solo por lote: el de
        # mayor severidad. Un mismo movimiento (p.ej. tilt lateral) dispara
        # AS-03 + AS-06 a ~100ms y dos pitidos encimados solo agregan ruido;
        # la detección de ambos queda en log y sync igual.
        _RANK = {"CRITICA": 4, "SEVERA": 3, "MODERADA": 2, "LEVE": 1, "INFO": 0}
        best = None  # (rank, alert_code, pattern)
        for event_data in events:
            try:
                alert_code = AlertCode(event_data.get("alert_code", "AS-01"))
            except ValueError:
                alert_code = AlertCode.AS_01
            # RNF-1.3: log inmediato (<1s tras confirmación) y sonido en
            # background para no bloquear el loop de captura.
            logger.info("Evento detectado: %s (%s)",
                        alert_code.value, event_data.get("message", ""))
            rank = _RANK.get(str(event_data.get("severity", "INFO")).upper(), 0)
            if best is None or rank > best[0]:
                try:
                    pattern = get_sound_pattern(self.ctx.config, alert_code)
                except Exception:
                    continue
                best = (rank, alert_code, pattern)
        if best is not None:
            # Árbitro temporal: SoundPlayer.play() corta al anterior, así que
            # un evento menor que llega junto a uno mayor lo "cancela".
            # Dentro de la ventana solo suena un rango igual o mayor; el menor
            # se loguea pero no suena (la detección queda intacta).
            try:
                window = float((self.ctx.config.detection_thresholds or {}).get(
                    "alert_priority_window_sec", 5.0))
            except (TypeError, ValueError):
                window = 5.0
            now_m = time.monotonic()
            last = getattr(self, "_last_priority_alert", None)
            if last is not None and (now_m - last[0]) < window and best[0] < last[1]:
                logger.debug("Sonido %s suprimido por prioridad (hay rango %d hace %.1fs)",
                             best[1].value, last[1], now_m - last[0])
                return
            self._last_priority_alert = (now_m, best[0])
            try:
                loop = asyncio.get_running_loop()
                loop.create_task(self._play_alert_background(best[2], best[1]))
            except RuntimeError:
                pass

    async def _play_alert_background(self, pattern, alert_code) -> None:
        try:
            await self.ctx.sound_player.play(pattern)
        except Exception as e:
            logger.debug("Alerta %s no sonó: %s", alert_code, e)

    async def _enqueue_events(self, events: list, frame=None) -> int:
        """HU-DEVICE-003 AC-001/AC-007: persiste cada evento + evidencia local.

        - Genera ``event_id`` UUID v7 si falta (idempotencia global).
        - Guarda 1 frame JPEG si severidad >= MODERADA (no bloquea).
        - ``evidence_path`` en DB es RELATIVO ``media/<id>.jpg`` (portable;
          se resuelve a <data_dir>/... solo al subir/borrar).
        - INSERT en ``pending_events`` (WAL, sobrevive reinicio).
        Retorna nº encolados. Sin buffer (tests legacy) = 0 sin error.
        """
        buf = getattr(self.ctx, "buffer", None)
        if buf is None:
            return 0
        try:
            from app.capture.evidence import save_event_frame
        except Exception:
            save_event_frame = None  # type: ignore
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        device_id = getattr(self.ctx.identity, "device_id", "") or ""
        try:
            ddir = data_dir()
        except Exception:
            ddir = None
        enqueued = 0
        for ev in events:
            if not isinstance(ev, dict):
                continue
            try:
                eid = str(ev.get("event_id") or new_event_id())
                ev.setdefault("event_id", eid)
                severity = str(ev.get("severity", "INFO"))
                evidence_path = None
                if save_event_frame is not None and ddir is not None:
                    try:
                        if loop is not None:
                            # cv2.imwrite bloquea ~10-30ms: va a executor.
                            evidence_path = await loop.run_in_executor(
                                None, lambda e=eid, s=severity, f=frame:
                                save_event_frame(f, e, s, ddir))
                        else:
                            evidence_path = save_event_frame(frame, eid, severity, ddir)
                    except Exception:
                        evidence_path = None
                telemetry = build_telemetry_event(
                    ev, device_id=device_id, event_id=eid,
                    has_evidence=bool(evidence_path))
                # has_evidence real para fase 2 (el detector no lo sabe).
                if evidence_path:
                    telemetry["has_evidence"] = True
                ok = buf.enqueue(eid, telemetry, evidence_path)
                if ok:
                    enqueued += 1
            except Exception as e:
                logger.debug("Enqueue evento fallido: %s", e)
                continue
        if enqueued:
            logger.debug("Buffer: %d evento(s) encolados", enqueued)
        return enqueued

    async def _heartbeat_loop(self) -> None:
        while self.ctx.running:
            interval = self.ctx.config.heartbeat_interval_sec
            await self._sleep_interruptible(max(5, int(interval)))
            if not self.ctx.running:
                break
            if self.ctx.identity.has_credentials():
                await self._heartbeat_once()
            else:
                logger.debug("Heartbeat local: estado=%s (sin credenciales backend)",
                             self.ctx.current_state.value)
                await self._maybe_background_register()

    async def _sleep_interruptible(self, total_sec: int) -> None:
        for _ in range(total_sec):
            if not self.ctx.running or self._shutdown.is_set():
                break
            await asyncio.sleep(1.0)

    async def _maybe_background_register(self) -> None:
        """Reintento de self-register en background (modo local + token)."""
        token = (self.ctx.env_config.get("provision_token", "") or "").strip()
        if not token:
            return
        if time.monotonic() - self.ctx.last_register_attempt < SELF_REGISTER_BG_RETRY_SEC:
            return
        logger.info("Reintentando auto-registro en background...")
        await self._register_device()

    def _heartbeat_payload(self) -> HeartbeatPayload:
        try:
            usage = shutil.disk_usage(data_dir())
            free_pct = int(usage.free * 100 / usage.total) if usage.total else 100
        except OSError:
            free_pct = 100
        uptime = int(time.monotonic() - self.ctx.boot_start_monotonic)
        # HU-DEVICE-003 AC-002/AC-006: pending real del buffer (0 si no hay).
        try:
            buf = getattr(self.ctx, "buffer", None)
            pending = int(buf.count_pending()) if buf is not None else 0
        except Exception:
            pending = 0
        return HeartbeatPayload(
            device_id=self.ctx.identity.device_id,
            firmware_version=self.ctx.identity.firmware_version,
            pending_count=max(0, pending),
            free_disk_pct=free_pct,
            uptime_s=uptime,
        )

    # -- HU-DEVICE-003: sync automático + retención + storage ------------
    async def _sync_loop(self) -> None:
        """HEAD cada sync_interval_sec; online → sync lote (AC-002/AC-003)."""
        # Intervalo inicial para no competir con el arranque.
        await asyncio.sleep(5.0)
        while self.ctx.running:
            try:
                interval = int(getattr(self.ctx.config, "sync_interval_sec", 30) or 30)
            except (TypeError, ValueError):
                interval = 30
            interval = min(3600, max(5, interval))
            await self._sleep_interruptible(interval)
            if not self.ctx.running:
                break
            try:
                await self._check_storage_crit()
            except Exception as e:
                logger.debug("Storage check fallido: %s", e)
            try:
                await self._run_retention_if_due()
            except Exception as e:
                logger.debug("Retención fallida: %s", e)
            if not self.ctx.identity.has_credentials():
                continue
            eng = getattr(self.ctx, "sync_engine", None)
            if eng is None:
                continue
            # Puerta de conectividad: HEAD sin auth (no confunde con heartbeat).
            try:
                online = await self.ctx.backend.healthcheck()
            except Exception:
                online = False
            # Actualiza monitor (dispara reset backoff en OFFLINE→ONLINE).
            try:
                conn = getattr(self.ctx, "connectivity", None)
                if conn is not None:
                    conn.online = True if online else False
                    if online and getattr(conn, "_was_offline", False):
                        self._on_reconnected()
                    conn._was_offline = not online
            except Exception:
                pass
            if not online:
                if getattr(self.ctx, "backend_available", True):
                    await self._enter_offline("healthcheck sin respuesta")
                continue
            await self._do_sync_batch()

    async def _do_sync_batch(self) -> dict:
        """Un lote 100 → POST → limpieza ACK. Retorna resumen (nunca lanza)."""
        eng = getattr(self.ctx, "sync_engine", None)
        if eng is None or not self.ctx.identity.has_credentials():
            return {"synced": 0, "skipped": True}
        try:
            retention = int(getattr(self.ctx.config, "retention_days", 7) or 7)
        except (TypeError, ValueError):
            retention = 7
        try:
            res = await eng.sync_once(
                self.ctx.identity.device_id, self.ctx.identity.api_key,
                retention_days=retention)
        except Exception as e:
            logger.debug("Sync batch fallido: %s", e)
            return {"synced": 0, "error": str(e)}
        # Si había OFFLINE y el sync pasó, el próximo heartbeat recupera estado.
        if res.get("synced"):
            self.ctx.backend_available = True
        return res

    async def _check_storage_crit(self) -> None:
        """AC-006: uso >90% → AS-09 (cooldown 1/día) + purga FAILED antiguos."""
        try:
            usage = shutil.disk_usage(data_dir())
            used_pct = 100 - int(usage.free * 100 / usage.total) if usage.total else 0
        except OSError:
            return
        if used_pct < STORAGE_CRIT_USED_PCT:
            return
        buf = getattr(self.ctx, "buffer", None)
        if buf is not None:
            try:
                buf.purge_old_failed(retention_days=0)  # FAILED más antiguos primero
            except Exception:
                pass
            try:
                from pathlib import Path as _P
                buf.purge_old_evidence_files(_P(data_dir()) / "media", retention_days=7)
            except Exception:
                pass
        now_m = time.monotonic()
        last = float(getattr(self.ctx, "last_storage_alert_monotonic", 0.0) or 0.0)
        if (now_m - last) < STORAGE_ALERT_COOLDOWN_SEC:
            return
        self.ctx.last_storage_alert_monotonic = now_m
        logger.warning("Almacenamiento al %d%% (>90%%): AS-09 + purga FAILED/evidencia 7d",
                       used_pct)
        try:
            await self._play_error_alert()
        except Exception:
            pass

    async def _run_retention_if_due(self) -> None:
        """AC-005/AC-006: purga diaria FAILED 7d + JPGs huérfanos/antiguos."""
        now_m = time.monotonic()
        last = float(getattr(self.ctx, "last_retention_monotonic", 0.0) or 0.0)
        if (now_m - last) < RETENTION_CHECK_SEC and last > 0:
            return
        self.ctx.last_retention_monotonic = now_m
        buf = getattr(self.ctx, "buffer", None)
        if buf is None:
            return
        try:
            retention = int(getattr(self.ctx.config, "retention_days", 7) or 7)
        except (TypeError, ValueError):
            retention = 7
        try:
            n = buf.purge_old_failed(retention_days=retention)
            if n:
                logger.info("Retención: %d FAILED >%dd purgados", n, retention)
        except Exception:
            pass
        try:
            from pathlib import Path as _P
            m = buf.purge_old_evidence_files(_P(data_dir()) / "media", retention_days=retention)
            if m:
                logger.info("Retención: %d JPGs purgados", m)
        except Exception:
            pass

    async def _heartbeat_once(self) -> None:
        payload = self._heartbeat_payload().to_request_dict()
        try:
            body = await self.ctx.backend.heartbeat(
                self.ctx.identity.device_id, self.ctx.identity.api_key, payload)
        except BackendAuthError as e:
            # 401/403: key inválida (rotada) o device sin asignar. No es caída
            # de red: se informa y se sigue operando en local hasta admin.
            self.ctx.backend_available = True
            logger.error("Heartbeat rechazado (%s). Si la key fue rotada, reprovisionar "
                         "vía PATCH /devices/{id}/rotate-key.", e)
            return
        except BackendError as e:
            self.ctx.backend_available = False
            await self._enter_offline(str(e))
            return

        self.ctx.backend_available = True
        parsed = self.ctx.backend.parse_heartbeat(body)
        self._apply_backend_status(parsed["status"])
        if self.ctx.current_state == DeviceState.OFFLINE:
            await self._recover_from_offline()
        # Config solo manual: el backend avisa via config_pending en el heartbeat
        pending = parsed.get("config_pending") or parsed.get("configPending") or parsed.get("pending_config_update")
        if pending is True or str(pending).lower() == "true":
            logger.info("Heartbeat avisa config pendiente: pull manual")
            await self._pull_remote_config("pending-flag")
        else:
            logger.debug("Heartbeat OK: backend=%s local=%s pending=%s",
                         parsed["status"], self.ctx.current_state.value, pending)

    async def shutdown(self) -> None:
        logger.info("Apagando dispositivo...")
        self.ctx.running = False
        self._shutdown.set()
        for task in self._tasks:
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)

        if self.ctx.camera:
            try:
                await self.ctx.camera.stop()
            except Exception as e:
                logger.debug("Error deteniendo cámara: %s", e)
        detector = self.ctx.detector
        landmarker = getattr(detector, "_landmark_detector", None) if detector else None
        if landmarker and hasattr(landmarker, "close"):
            try:
                landmarker.close()
            except Exception as e:
                logger.debug("Error cerrando landmarker: %s", e)
        logger.info("Dispositivo apagado")


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    manager = DeviceManager()
    try:
        await manager.initialize()
        await manager.run()
    except KeyboardInterrupt:
        logger.info("Interrumpido por usuario")
    except CameraError:
        logger.error("Arranque abortado por fallo de cámara")
        sys.exit(1)
    except Exception as e:
        logger.exception("Error fatal: %s", e)
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
