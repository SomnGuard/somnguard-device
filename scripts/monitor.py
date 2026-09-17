"""Monitor silencioso con preview — ve qué detecta SIN que suene ninguna alerta.

Uso típico (calibrar bostezos falsos):
    py scripts/monitor.py
    py scripts/monitor.py --show --interval 0.5 --csv data/monitor_log.csv
    py scripts/monitor.py --no-show --interval 1.0

Garantía: este script JAMÁS importa ni llama a SoundPlayer. Los eventos se
muestran como [EVENTO] en consola/CSV pero no suenan.

Qué ves cada --interval segundos:
- cara / fov / obstrucción (s conStages 5s/20s/30s)
- EAR, MAR, tilt, yaw/pitch, gaze OFF + calibración
- progreso temporizado: "ojos 1.2/2.0s", "bostezo 1.8/2.5s pico 0.95 ratio 0.8",
  "bostezos 1/2 en 5min", "tilt 12/20° 0.5/3.0s", "gaze OFF 1.1/3.0s", etc.
  Esto responde a "algunos es por tiempo": ves cuánto le falta a cada evento.

Preview (--show): ventana con MAR/EAR/estado + bbox. ESC sale.
CSV: una fila por frame para analizar después.

Usa la MISMA config que app.main (default -> cache -> override local),
así lo que ves aquí es lo que decidiría el device real.
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.analysis.ear_mar import (
    estimate_head_pose,
    eye_aspect_ratio,
    head_tilt_deg,
    is_head_pose_plausible,
    mouth_aspect_ratio,
)
from app.analysis.landmarks import get_key_points
from app.common.config import load_cached_remote, load_default_config, load_local_override
from app.device.identity import data_dir


def load_effective_thresholds() -> dict:
    base = load_default_config()
    try:
        dd = data_dir()
    except Exception:
        dd = Path("data")
    cfg = load_local_override(load_cached_remote(base, dd), dd)
    return dict(cfg.detection_thresholds or {})


def fmt_progress(cur: float, need: float, unit: str = "s") -> str:
    pct = min(1.0, cur / need) if need > 0 else 0.0
    bar = "#" * int(pct * 10) + "-" * (10 - int(pct * 10))
    return f"{cur:.1f}/{need:.1f}{unit}[{bar}]"


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--show", action="store_true", help="abrir preview con overlay (ESC sale)")
    ap.add_argument("--no-show", action="store_true", help="solo consola+CSV")
    ap.add_argument("--interval", type=float, default=0.5, help="cada cuántos s imprime resumen")
    ap.add_argument("--seconds", type=float, default=300.0)
    ap.add_argument("--csv", default="data/monitor_log.csv")
    ap.add_argument("--camera", type=int, default=0)
    args = ap.parse_args()
    show = args.show and not args.no_show
    # Por defecto en Windows sin flags: mostrar preview (es lo que pediste).
    if not args.show and not args.no_show:
        show = True

    try:
        import cv2
    except ImportError:
        print("Falta opencv-python: pip install opencv-python")
        return

    thresholds = load_effective_thresholds()
    from app.analysis.pipeline import VisionPipeline

    pipe = VisionPipeline(thresholds)
    try:
        await pipe.load_models()
    except Exception as e:
        print(f"Aviso: modelos parcialmente disponibles ({e})")
    som = pipe.somnolence
    dis = pipe.distraction
    obs = pipe.obstruction
    print(f"Umbrales bostezo: open={som.yawn_open:.2f} close={som.yawn_close:.2f} "
          f"pico>={som.yawn_peak_min:.2f} dur {som.yawn_min_dur:.1f}-{som.yawn_max_dur:.1f}s "
          f"ratio>={som.yawn_min_open_ratio:.2f} cooldown={som.yawn_cooldown:.0f}s "
          f"necesarios={som.yawn_min}/ventana {som.yawn_window/60:.0f}min")
    print("MODO SILENCIOSO: ningún evento sonará. Solo se muestra/loguea.")

    cap = cv2.VideoCapture(args.camera)
    if not cap.isOpened():
        print(f"No se pudo abrir la cámara {args.camera}")
        return

    csv_path = Path(args.csv)
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    f = open(csv_path, "w", newline="", encoding="utf-8")
    wr = csv.writer(f)
    wr.writerow(["t_sec", "face", "fov", "ear", "mar", "tilt_deg", "yaw", "pitch",
                 "gaze_off", "phone", "moving", "eye_dur", "yawn_dur", "yawn_peak",
                 "yawn_ratio", "yawns_win", "tilt_dur", "gaze_dur", "obstr_sec", "events"])

    t0 = time.monotonic()
    last_print = 0.0
    print("Mira al frente 10s, habla 20s, abre grande 3s (bostezo simulado). ESC sale.")
    try:
        while True:
            ok, frame = cap.read()
            if not ok or frame is None:
                await asyncio.sleep(0.1)
                continue
            now = time.monotonic()
            if now - t0 > args.seconds:
                break
            t_cap = now
            result = await pipe.process(frame, capture_time=t_cap)
            lm = result.landmarks
            # Métricas para display (el pipeline no las expone: se recalculan).
            ear = mar = tilt = yaw = pitch = 0.0
            if lm is not None:
                try:
                    kp = get_key_points(lm.landmarks)
                    ear = (eye_aspect_ratio(kp["left_eye"])
                           + eye_aspect_ratio(kp["right_eye"])) / 2.0
                    mar = mouth_aspect_ratio(kp["mouth"])
                    pitch, yaw, _ = estimate_head_pose(lm.landmarks, lm.image_shape)
                    if is_head_pose_plausible(pitch, yaw,
                                              som.pose_yaw_limit, som.pose_pitch_limit):
                        tilt = head_tilt_deg(pitch, yaw)
                    else:
                        tilt = 0.0
                except Exception:
                    pass
            s = som.debug_state(now)
            try:
                d = dis.debug_state(now)
            except Exception:
                d = {}
            obstr = 0.0
            try:
                obstr = float(obs.obstruction_elapsed_sec)
            except Exception:
                pass
            # phone/moving puntuales (el debug_state ya trae duraciones).
            phone_now = bool(d.get("phone_active")) if d else False
            gaze_off = bool(d.get("gaze_off")) if d else False
            move_now = bool(d.get("move_active")) if d else False
            ev_ids = [e.get("event_type_id", "?") for e in (result.events or [])]
            for e in result.events or []:
                print(f"*** [EVENTO-silencioso] {e.get('event_type_id')} "
                      f"{e.get('alert_code','')} :: {e.get('message','')}")
            wr.writerow([f"{now-t0:.2f}", int(result.face_present), int(result.fov_ok),
                         f"{ear:.3f}", f"{mar:.3f}", f"{tilt:.1f}", f"{yaw:.1f}",
                         f"{pitch:.1f}", int(gaze_off), int(phone_now), int(move_now),
                         s.get("eye_dur_sec"), s.get("yawn_dur_sec"), s.get("yawn_peak"),
                         s.get("yawn_open_ratio"), s.get("yawns_in_window"),
                         s.get("tilt_dur_sec"), d.get("gaze_dur_sec") if d else "",
                         f"{obstr:.1f}", ";".join(ev_ids)])
            if now - last_print >= args.interval:
                last_print = now
                calib = "" if d.get("gaze_calibrated", True) else "(gaze calibrando) "
                print(f"t={now-t0:5.1f}s cara={int(result.face_present)} fov={int(result.fov_ok)} "
                      f"EAR={ear:.2f} MAR={mar:.2f} tilt={tilt:.0f}° yaw={yaw:+.0f} pit={pitch:+.0f} {calib}")
                print(f"  ojos {fmt_progress(s['eye_dur_sec'], s['eye_need_sec'])} "
                      f"| bostezo {fmt_progress(s['yawn_dur_sec'], s['yawn_need_sec'])} "
                      f"pico {s['yawn_peak']:.2f}/{s['yawn_peak_need']:.2f} "
                      f"ratio {s['yawn_open_ratio']:.2f}/{s['yawn_ratio_need']:.2f} "
                      f"ventana {s['yawns_in_window']}/{s['yawns_need']} "
                      f"{'BLOQ-cierre' if s['yawn_blocked_until_close'] else ''}")
                print(f"  cabeceo {tilt:.0f}/{som.tilt_deg:.0f}° "
                      f"{fmt_progress(s['tilt_dur_sec'], s['tilt_need_sec'])} "
                      f"| gaze {'OFF' if gaze_off else 'on '} "
                      f"{fmt_progress(d.get('gaze_dur_sec', 0.0), d.get('gaze_need_sec', 3.0))} "
                      f"| phone {fmt_progress(d.get('phone_dur_sec', 0.0), d.get('phone_need_sec', 2.0))} "
                      f"| mov {fmt_progress(d.get('move_dur_sec', 0.0), d.get('move_need_sec', 3.0))} "
                      f"| obstr {obstr:.0f}s / PERCLOS {s['perclos']:.2f}")
            if show:
                try:
                    h, w = frame.shape[:2]
                    cv2.putText(frame, f"MAR {mar:.2f} EAR {ear:.2f} tilt {tilt:.0f}",
                                (10, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 0), 2)
                    cv2.putText(frame,
                                f"yawn {s['yawn_dur_sec']:.1f}/{s['yawn_need_sec']:.1f}s "
                                f"win {s['yawns_in_window']}/{s['yawns_need']}",
                                (10, 58), cv2.FONT_HERSHEY_SIMPLEX, 0.7,
                                (0, 0, 255) if s["yawning"] else (0, 255, 0), 2)
                    cv2.putText(frame,
                                f"ojos {s['eye_dur_sec']:.1f}s gaze {'OFF' if gaze_off else 'on'} "
                                f"fov {int(result.fov_ok)}",
                                (10, 86), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 0), 2)
                    if ev_ids:
                        cv2.putText(frame, f"EVENTO: {','.join(ev_ids)} (silencio)",
                                    (10, 114), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 0, 255), 2)
                    if lm is not None:
                        try:
                            xs = (lm.landmarks[:, 0] * w).astype(int)
                            ys = (lm.landmarks[:, 1] * h).astype(int)
                            cv2.rectangle(frame, (xs.min(), ys.min()),
                                          (xs.max(), ys.max()), (255, 0, 0), 1)
                        except Exception:
                            pass
                    cv2.imshow("SomnGuard monitor (silencioso) - ESC sale", frame)
                    if cv2.waitKey(1) & 0xFF == 27:
                        break
                except Exception:
                    pass
    finally:
        f.close()
        cap.release()
        try:
            import cv2 as _cv
            _cv.destroyAllWindows()
        except Exception:
            pass
        print(f"CSV guardado en {csv_path}")


if __name__ == "__main__":
    asyncio.run(main())
