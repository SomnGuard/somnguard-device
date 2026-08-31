from __future__ import annotations
import asyncio
import logging
import signal
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from app.common.models import (
    DeviceConfig, DeviceIdentity, DeviceState, AlertCode, HeartbeatPayload
)
from app.common.config import load_default_config, load_env_config, merge_configs, get_sound_pattern
from app.device.identity import load_or_create_identity, save_identity, update_device_id, update_state
from app.capture.camera import Camera, CameraError
from app.analysis.detector import Detector, DetectionResult
from app.monitoring.presence import PresenceMonitor
from app.alerting.sound_player import SoundPlayer


logger = logging.getLogger(__name__)


@dataclass
class DeviceContext:
    identity: DeviceIdentity
    config: DeviceConfig
    env_config: dict
    camera: Optional[Camera] = None
    detector: Optional[Detector] = None
    presence: Optional[PresenceMonitor] = None
    sound_player: Optional[SoundPlayer] = None
    running: bool = False
    last_face_seen: Optional[datetime] = None
    current_state: DeviceState = DeviceState.REGISTRADO
    backend_available: bool = True


class DeviceManager:
    def __init__(self):
        self.ctx = DeviceContext(
            identity=load_or_create_identity(),
            config=load_default_config(),
            env_config=load_env_config(),
        )
        self._shutdown = asyncio.Event()
        self._tasks: list[asyncio.Task] = []

    async def initialize(self) -> None:
        logger.info("Inicializando dispositivo %s", self.ctx.identity.serial_number)

        self.ctx.sound_player = SoundPlayer(self.ctx.config)
        await self._init_camera()
        await self._init_detector()
        await self._init_presence()

        await self._register_device()
        await self._apply_config()

        # Detección inicial para establecer estado ACTIVO/ESPERA
        await self._initial_face_check()

        self.ctx.running = True
        logger.info("Dispositivo inicializado correctamente")

    async def _init_camera(self) -> None:
        try:
            self.ctx.camera = Camera(
                resolution=self.ctx.config.camera_resolution,
                fps=self.ctx.config.camera_fps,
            )
            await self.ctx.camera.start()
            await self._play_startup_alert()
            logger.info("Cámara inicializada: %s", self.ctx.config.camera_resolution)
        except CameraError as e:
            logger.error("Error inicializando cámara: %s", e)
            await self._play_error_alert()
            raise

    async def _play_startup_alert(self) -> None:
        pattern = get_sound_pattern(self.ctx.config, AlertCode.AS_08)
        await self.ctx.sound_player.play(pattern)

    async def _play_error_alert(self) -> None:
        pattern = get_sound_pattern(self.ctx.config, AlertCode.AS_09)
        await self.ctx.sound_player.play(pattern)

    async def _init_detector(self) -> None:
        self.ctx.detector = Detector(self.ctx.config.detection_thresholds)
        await self.ctx.detector.load_model()
        logger.info("Detector cargado")

    async def _init_presence(self) -> None:
        self.ctx.presence = PresenceMonitor(face_absence_timeout_sec=30)
        self.ctx.presence.on_state_change = self._on_presence_change
        logger.info("Monitor de presencia iniciado")

    def _on_presence_change(self, face_present: bool) -> None:
        now = datetime.utcnow()
        if face_present:
            self.ctx.last_face_seen = now
            if self.ctx.current_state == DeviceState.ESPERA:
                asyncio.create_task(self._transition_to(DeviceState.ACTIVO))
        else:
            self.ctx.last_face_seen = None
            if self.ctx.current_state == DeviceState.ACTIVO:
                asyncio.create_task(self._transition_to(DeviceState.ESPERA))

    async def _initial_face_check(self) -> None:
        frame = await self.ctx.camera.read_frame()
        if frame is not None:
            face_present = await self.ctx.detector.detect_face_only(frame)
            self.ctx.presence.update(face_present)
            if face_present:
                await self._transition_to(DeviceState.ACTIVO)
            else:
                await self._transition_to(DeviceState.ESPERA)
        else:
            await self._transition_to(DeviceState.ESPERA)

    async def _register_device(self) -> None:
        if self.ctx.identity.device_id:
            logger.info("Dispositivo ya registrado: %s", self.ctx.identity.device_id)
            await self._transition_to(DeviceState.ASIGNADO)
            return

        # Modo local por defecto (sin backend)
        local_id = f"local-{self.ctx.identity.serial_number}"
        self.ctx.identity = update_device_id(self.ctx.identity, local_id)
        self.ctx.backend_available = False
        await self._transition_to(DeviceState.ASIGNADO)
        logger.info("Dispositivo en modo local: %s", local_id)

    async def _apply_config(self) -> None:
        if not self.ctx.backend_available:
            return
        # TODO: cuando HU-API-006 esté lista, implementar GET /devices/{id}/config
        pass

    async def _reconfigure_services(self) -> None:
        if self.ctx.detector:
            self.ctx.detector.update_thresholds(self.ctx.config.detection_thresholds)
        if self.ctx.sound_player:
            self.ctx.sound_player.update_config(self.ctx.config)
        if self.ctx.camera and self.ctx.config.camera_resolution:
            await self.ctx.camera.set_resolution(self.ctx.config.camera_resolution)

    async def _transition_to(self, new_state: DeviceState) -> None:
        if self.ctx.current_state == new_state:
            return
        logger.info("Cambio de estado: %s -> %s", self.ctx.current_state, new_state)
        self.ctx.current_state = new_state
        self.ctx.identity = update_state(self.ctx.identity, new_state)

    async def run(self) -> None:
        loop = asyncio.get_running_loop()
        if sys.platform != "win32":
            for sig in (signal.SIGTERM, signal.SIGINT):
                loop.add_signal_handler(sig, self._shutdown.set)

        self._tasks = [
            asyncio.create_task(self._capture_loop()),
            asyncio.create_task(self._heartbeat_loop()),
        ]

        await self._shutdown.wait()
        await self.shutdown()

    async def _capture_loop(self) -> None:
        frame_interval = 1.0 / self.ctx.config.camera_fps
        while self.ctx.running:
            start = time.monotonic()
            try:
                if self.ctx.current_state == DeviceState.ACTIVO:
                    frame = await self.ctx.camera.read_frame()
                    if frame is not None:
                        result = await self.ctx.detector.process(frame)
                        await self._handle_detection_result(result, frame)
                elif self.ctx.current_state == DeviceState.ESPERA:
                    frame = await self.ctx.camera.read_frame()
                    if frame is not None:
                        face_present = await self.ctx.detector.detect_face_only(frame)
                        self.ctx.presence.update(face_present)
                else:
                    await asyncio.sleep(1.0)
            except Exception as e:
                logger.error("Error en loop de captura: %s", e)
                await asyncio.sleep(1.0)

            elapsed = time.monotonic() - start
            sleep_time = max(0, frame_interval - elapsed)
            if sleep_time > 0:
                await asyncio.sleep(sleep_time)

    async def _handle_detection_result(self, result, frame) -> None:
        if isinstance(result, DetectionResult):
            self.ctx.presence.update(result.face_present and result.fov_ok)
            events = result.events
        else:
            events = result.get("events", []) if isinstance(result, dict) else []

        if not events:
            return

        for event_data in events:
            alert_code = AlertCode(event_data.get("alert_code", "AS-01"))
            pattern = get_sound_pattern(self.ctx.config, alert_code)
            await self.ctx.sound_player.play(pattern)
            logger.info("Evento detectado: %s", alert_code)

    async def _heartbeat_loop(self) -> None:
        interval = self.ctx.config.heartbeat_interval_sec
        while self.ctx.running:
            await asyncio.sleep(interval)
            if self.ctx.running:
                # Log de heartbeat local (sin backend)
                logger.debug("Heartbeat: state=%s, backend=%s", self.ctx.current_state, self.ctx.backend_available)

    async def shutdown(self) -> None:
        logger.info("Apagando dispositivo...")
        self.ctx.running = False
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)

        if self.ctx.camera:
            await self.ctx.camera.stop()
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
    except Exception as e:
        logger.exception("Error fatal: %s", e)
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())