"""Orquestación del dispositivo: inicialización, estados y backend (HU-DEVICE-002).

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


logger = logging.getLogger(__name__)

# Presupuesto de arranque RNF-1.1 (AC-001) y cotas para no bloquearlo.
BOOT_BUDGET_SEC = 60.0
SELF_REGISTER_BOOT_ATTEMPTS = 2
SELF_REGISTER_RETRY_DELAY_SEC = 2.0
SELF_REGISTER_BG_RETRY_SEC = 60.0
INITIAL_FACE_CHECK_FRAMES = 3
DETECTOR_RETRY_SEC = 30.0


@dataclass
class DeviceContext:
    identity: DeviceIdentity
    config: DeviceConfig
    env_config: dict
    camera: Optional[Camera] = None
    detector: Optional[Detector] = None
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


class DeviceManager:
    def __init__(self):
        identity = load_or_create_identity()
        env_config = load_env_config()
        config = load_local_override(load_default_config(), data_dir())
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
        self._shutdown = asyncio.Event()
        self._tasks: list[asyncio.Task] = []

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
        await self._timed("config remota", self._pull_remote_config, "arranque")

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
        """Chequeo inicial multi-frame de rostro y campo visual (AC-002/AC-003)."""
        if not self.ctx.camera or not self.ctx.detector or not self.ctx.detect_available:
            logger.info("Chequeo inicial omitido (cámara/detector no disponible); estado ESPERA")
            self.ctx.presence.mark_absent()
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
            logger.warning("Chequeo inicial sin frames; estado ESPERA")
            self.ctx.presence.mark_absent()
            await self._transition_to(DeviceState.ESPERA)
            return
        if faces > 0:
            if fov_bad:
                logger.warning("Campo visual deficiente en arranque (mala posición/obstrucción); "
                               "se inicia en ACTIVO con escalamiento AS-09")
            self.ctx.presence.mark_present()
            await self._transition_to(DeviceState.ACTIVO)
        else:
            self.ctx.presence.mark_absent()
            await self._transition_to(DeviceState.ESPERA)

    # -- registro / provisioning (AC-007) --------------------------------
    async def _register_device(self) -> None:
        if self.ctx.identity.has_credentials():
            logger.info("Dispositivo ya registrado: %s", self.ctx.identity.device_id)
            self.ctx.backend_available = True
            if self.ctx.current_state == DeviceState.REGISTRADO:
                await self._transition_to(DeviceState.ASIGNADO)
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
            await self._transition_to(DeviceState.ASIGNADO)
            logger.info("Auto-registro completado: device_id=%s estado=%s",
                        device_id, parsed["status"])
            return True
        # 200 = reintento idempotente sin reexposición de secretos (ADR-010).
        if self.ctx.identity.has_credentials():
            logger.info("Self-register idempotente (200): credenciales locales vigentes, device_id=%s",
                        self.ctx.identity.device_id)
            self.ctx.backend_available = True
            await self._transition_to(DeviceState.ASIGNADO)
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
        if self.ctx.current_state == DeviceState.REGISTRADO:
            await self._transition_to(DeviceState.ASIGNADO)

    # -- config remota (AC-005) ------------------------------------------
    async def _pull_remote_config(self, reason: str) -> bool:
        if not self.ctx.identity.has_credentials():
            return False
        try:
            remote = await self.ctx.backend.fetch_config(
                self.ctx.identity.device_id, self.ctx.identity.api_key)
        except BackendAuthError as e:
            logger.warning("GET config rechazado (key inválida?): %s", e)
            return False
        except BackendError as e:
            logger.debug("GET config fallido (%s): %s", reason, e)
            return False
        if not remote:
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
        try:
            self.ctx.identity = update_state(self.ctx.identity, new_state)
        except OSError as e:
            logger.debug("No se pudo persistir estado: %s", e)

    async def _enter_offline(self, reason: str) -> None:
        if self.ctx.current_state in (DeviceState.ACTIVO, DeviceState.ESPERA):
            self.ctx.pre_offline_state = self.ctx.current_state
            await self._transition_to(DeviceState.OFFLINE)
            logger.warning("Sin conectividad (%s): detección pausada, estado OFFLINE", reason)

    async def _recover_from_offline(self) -> None:
        if self.ctx.current_state == DeviceState.OFFLINE:
            target = (DeviceState.ACTIVO if self.ctx.presence.face_present
                      else self.ctx.pre_offline_state)
            if target not in (DeviceState.ACTIVO, DeviceState.ESPERA):
                target = DeviceState.ACTIVO if self.ctx.presence.face_present else DeviceState.ESPERA
            await self._transition_to(target)
            logger.info("Conectividad restaurada: estado %s", target.value)

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
                if state in (DeviceState.OFFLINE, DeviceState.SUSPENDIDO,
                             DeviceState.RETIRADO, DeviceState.ERROR):
                    await asyncio.sleep(1.0)
                    continue
                if not self.ctx.detect_available:
                    await self._maybe_retry_detector()
                    await asyncio.sleep(1.0)
                    continue
                if state == DeviceState.ACTIVO:
                    frame = await self.ctx.camera.read_frame()
                    if frame is not None:
                        result = await self.ctx.detector.process(frame)
                        await self._handle_detection_result(result)
                        await self._check_obstruction_pause()
                elif state in (DeviceState.ESPERA, DeviceState.ASIGNADO, DeviceState.REGISTRADO):
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
        """Obstrucción > 30s: AS-09 + pausa de detección (ESPERA) — AC-002."""
        detector = self.ctx.detector
        if (detector and detector.needs_detection_pause
                and self.ctx.current_state == DeviceState.ACTIVO):
            logger.warning("Campo visual obstruido >30s: pausa de detección (ESPERA)")
            await self._play_error_alert()
            await self._transition_to(DeviceState.ESPERA)

    async def _handle_detection_result(self, result, frame=None) -> None:
        if isinstance(result, DetectionResult):
            self.ctx.presence.update(result.face_present and result.fov_ok)
            events = result.events
        else:
            events = result.get("events", []) if isinstance(result, dict) else []

        if not events:
            return

        for event_data in events:
            try:
                alert_code = AlertCode(event_data.get("alert_code", "AS-01"))
            except ValueError:
                alert_code = AlertCode.AS_01
            pattern = get_sound_pattern(self.ctx.config, alert_code)
            await self.ctx.sound_player.play(pattern)
            logger.info("Evento detectado: %s (%s)",
                        alert_code.value, event_data.get("message", ""))

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
        return HeartbeatPayload(
            device_id=self.ctx.identity.device_id,
            firmware_version=self.ctx.identity.firmware_version,
            pending_count=0,  # HU-DEVICE-003: buffer offline informará aquí
            free_disk_pct=free_pct,
            uptime_s=uptime,
        )

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
        elif self.ctx.current_state == DeviceState.ASIGNADO and parsed["status"] == "DEVICE_ACTIVE":
            # Primer heartbeat válido posterior a asignación (ADR-010 §4).
            await self._transition_to(
                DeviceState.ACTIVO if self.ctx.presence.face_present else DeviceState.ESPERA)
        logger.debug("Heartbeat OK: backend=%s local=%s",
                     parsed["status"], self.ctx.current_state.value)
        await self._pull_remote_config("post-heartbeat")

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
