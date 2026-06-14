"""
Lane Tracker — real-time 3-lane detection for a jog-on-the-spot exercise game.

Opens one OpenCV window showing the live camera feed at all times. Overlaid on the
feed it always shows which of three lanes (Left / Center / Right) the user occupies,
updated every frame whether they are jogging or standing still. Lanes are dynamic:
they are measured relative to a rolling average of the user's own mid-hip position,
not fixed screen columns, so the system follows the user instead of forcing them to
stand in a marked spot. Jogging is detected separately as a secondary status badge
and never gates the lane display.

Pipeline (per frame): freshest camera frame -> downscale to PROCESS_WIDTH ->
MediaPipe Pose (lite) -> mid-hip landmark -> rolling-baseline lane decision +
debounce -> draw overlay/HUD on the full-resolution frame -> show.

Install:
    pip install mediapipe opencv-python numpy

Usage:
    python lane_tracker.py
    Stand ~2-3 m from the camera so your whole body (head, torso, feet) is in frame.
    Press Q to quit.
"""

# ----------------------------------------------------------------------------
# 1. IMPORTS AND CONFIG
# ----------------------------------------------------------------------------
import time                       # FPS timing + once-per-second console output
import threading                  # background camera-capture thread
from collections import deque     # fixed-length rolling buffers (O(1) push/pop)

import cv2                         # camera capture, drawing, window
import mediapipe as mp            # Pose landmark detection

# --- Tunable configuration (all knobs live here) ---------------------------
LANE_THRESHOLD              = 0.08      # half-width of the center lane, normalized (0..1 of frame width)
BASELINE_WINDOW            = 90        # frames of mid-hip X averaged into the drifting "center anchor"
DEBOUNCE_FRAMES            = 4         # consecutive frames in a new lane before the change is committed
HIP_BUFFER_SIZE            = 20        # frames of mid-hip Y kept for the jogging oscillation test
JOGGING_AMPLITUDE_THRESHOLD = 0.018    # peak-to-peak hip-Y (normalized) above which we call it "jogging"
OVERLAY_ALPHA              = 0.25      # translucency of the active-lane color fill
PROCESS_WIDTH              = 480       # width (px) the frame is shrunk to before Pose runs (speed)
DRAW_SKELETON              = False     # toggle to draw the full MediaPipe skeleton (debug)
WINDOW_NAME                = "Lane Tracker"

# --- Lane identity tables (lane ids: 1 = Left, 2 = Center, 3 = Right) -------
# OpenCV colors are BGR, not RGB.
LANE_COLORS = {1: (0, 255, 255),   # yellow
               2: (0, 255, 0),     # green
               3: (255, 0, 0)}     # blue
# Hershey fonts (the only ones cv2.putText can render) are ASCII-only, so we use
# ASCII arrows here instead of the ◀ ▶ glyphs — those would render as garbage.
LANE_LABELS = {1: "< LEFT", 2: "CENTER", 3: "RIGHT >"}
LANE_NAMES  = {1: "LEFT", 2: "CENTER", 3: "RIGHT"}   # plain names for the console log

mp_pose = mp.solutions.pose                 # Pose solution module
mp_drawing = mp.solutions.drawing_utils     # only used when DRAW_SKELETON is on


# ----------------------------------------------------------------------------
# 2. CAMERA THREAD — never let the main loop wait on the camera
# ----------------------------------------------------------------------------
class CameraThread:
    """Continuously grabs frames in a background thread and keeps only the most
    recent one. The main loop reads that latest frame instead of calling
    cap.read() itself, so processing never blocks on camera I/O (low latency)."""

    def __init__(self, src=0, width=1280, height=720):
        # CAP_DSHOW is the DirectShow backend; on Windows it opens far faster and
        # avoids the slow default MSMF backend.
        self.cap = cv2.VideoCapture(src, cv2.CAP_DSHOW)
        # BUFFERSIZE = 1 tells the driver to keep a single-frame queue, so a slow
        # consumer can never fall behind on a backlog of stale frames.
        self.cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        self.cap.set(cv2.CAP_PROP_FRAME_WIDTH, width)
        self.cap.set(cv2.CAP_PROP_FRAME_HEIGHT, height)

        self.lock = threading.Lock()   # guards latest_frame across the two threads
        self.latest_frame = None       # most recent frame (BGR numpy array)
        self.running = True
        # daemon=True so the thread dies automatically if the program exits.
        self.thread = threading.Thread(target=self._update, daemon=True)
        self.thread.start()

    def _update(self):
        """Loop forever, overwriting latest_frame with whatever just arrived."""
        while self.running:
            ok, frame = self.cap.read()
            if ok:
                with self.lock:        # brief lock only around the pointer swap
                    self.latest_frame = frame

    def read(self):
        """Return a private copy of the latest frame (or None if none yet).
        The copy lets the caller draw on it without holding the lock."""
        with self.lock:
            if self.latest_frame is None:
                return None
            return self.latest_frame.copy()

    def stop(self):
        """Stop the thread and release the camera device."""
        self.running = False
        self.thread.join(timeout=1.0)
        self.cap.release()


# ----------------------------------------------------------------------------
# 3. MID-HIP EXTRACTION
# ----------------------------------------------------------------------------
def get_mid_hip(landmarks, w, h):
    """Return the midpoint of the two hip landmarks.

    Returns (norm_x, norm_y, px_x, px_y) where norm_* are in 0..1 and px_* are
    pixels for a frame of size (w, h). Returns None if either hip is not
    confidently visible (so we don't track a phantom position)."""
    lh = landmarks[mp_pose.PoseLandmark.LEFT_HIP.value]
    rh = landmarks[mp_pose.PoseLandmark.RIGHT_HIP.value]
    if lh.visibility < 0.5 or rh.visibility < 0.5:   # both hips must be trustworthy
        return None
    nx = (lh.x + rh.x) / 2.0          # normalized mid-hip X (lateral position)
    ny = (lh.y + rh.y) / 2.0          # normalized mid-hip Y (used for bounce)
    return nx, ny, int(nx * w), int(ny * h)


# ----------------------------------------------------------------------------
# 4. LANE DECISION
# ----------------------------------------------------------------------------
def get_lane(offset, threshold):
    """Map a signed offset-from-baseline to a lane id. Left=1, Center=2, Right=3.
    (Image X grows rightward, so a negative offset means the user moved left.)"""
    if offset < -threshold:
        return 1     # left of the center band
    if offset > threshold:
        return 3     # right of the center band
    return 2         # within +/- threshold = center


# ----------------------------------------------------------------------------
# 5. JOGGING DETECTION
# ----------------------------------------------------------------------------
def is_jogging(y_buffer, threshold):
    """True if the hip bobbed up and down by more than `threshold` (normalized)
    across the buffer — i.e. peak-to-peak vertical amplitude exceeds the cutoff."""
    if len(y_buffer) < 2:                       # need at least two samples to span
        return False
    return (max(y_buffer) - min(y_buffer)) > threshold


# ----------------------------------------------------------------------------
# 6. LANE OVERLAY — boundary lines + active-lane highlight
# ----------------------------------------------------------------------------
def draw_overlay(frame, lane, baseline_px, threshold_px, alpha):
    """Draw the translucent fill for the active lane plus the two boundary lines.
    Mutates and returns `frame`."""
    h, w = frame.shape[:2]
    left_bound = int(baseline_px - threshold_px)    # x of the left/center divider
    right_bound = int(baseline_px + threshold_px)   # x of the center/right divider

    # --- translucent fill of just the active lane column ---
    overlay = frame.copy()                          # draw solid, then blend for alpha
    color = LANE_COLORS[lane]
    if lane == 1:                                   # left column: screen edge -> left line
        cv2.rectangle(overlay, (0, 0), (left_bound, h), color, -1)
    elif lane == 2:                                 # center column: between the lines
        cv2.rectangle(overlay, (left_bound, 0), (right_bound, h), color, -1)
    else:                                           # right column: right line -> screen edge
        cv2.rectangle(overlay, (right_bound, 0), (w, h), color, -1)
    # blend the colored copy over the original at `alpha` (the rest stays untouched
    # because overlay == frame everywhere we didn't draw)
    cv2.addWeighted(overlay, alpha, frame, 1.0 - alpha, 0, frame)

    # --- semi-opaque white boundary lines (visible over any background) ---
    line_overlay = frame.copy()
    cv2.line(line_overlay, (left_bound, 0), (left_bound, h), (255, 255, 255), 2)
    cv2.line(line_overlay, (right_bound, 0), (right_bound, h), (255, 255, 255), 2)
    # 0.7 weight -> ~70% opaque lines; untouched pixels are identical in both
    # images so they pass through unchanged.
    cv2.addWeighted(line_overlay, 0.7, frame, 0.3, 0, frame)
    return frame


# ----------------------------------------------------------------------------
# 7. HUD — lane label, jogging status, FPS, calibrating state
# ----------------------------------------------------------------------------
def draw_hud(frame, lane, jogging, fps, calibrating):
    """Draw all text labels on top of the (already overlaid) frame."""
    h, w = frame.shape[:2]
    font = cv2.FONT_HERSHEY_SIMPLEX

    # --- big lane label (or "Calibrating...") at top center ---
    if calibrating:
        label = "Calibrating..."
        label_color = (255, 255, 255)
    else:
        label = LANE_LABELS[lane]
        label_color = LANE_COLORS[lane]
    scale, thick = 1.8, 4
    (tw, th), base = cv2.getTextSize(label, font, scale, thick)   # measure for centering + bg
    x = max(10, (w - tw) // 2)                                    # clamp so it never goes off-screen
    y = th + 20                                                   # baseline of the text
    # filled black rectangle behind the text so it stays legible over the camera image
    cv2.rectangle(frame, (x - 15, y - th - 15), (x + tw + 15, y + base + 10), (0, 0, 0), -1)
    cv2.putText(frame, label, (x, y), font, scale, label_color, thick, cv2.LINE_AA)

    # --- jogging status badge just below the lane label ---
    if not calibrating:
        sy = y + base + 40                                       # a little below the label block
        dot_color = (0, 255, 0) if jogging else (160, 160, 160)  # green when jogging, gray when still
        status_text = "jogging" if jogging else "still"
        # filled circle stands in for the ●/○ glyph (Hershey font can't draw those)
        cv2.circle(frame, (x + 10, sy - 6), 8, dot_color, -1)
        cv2.putText(frame, status_text, (x + 28, sy), font, 0.7, dot_color, 2, cv2.LINE_AA)

    # --- FPS counter, top-right corner ---
    fps_text = f"{fps:.0f} fps"
    (fw, fh), _ = cv2.getTextSize(fps_text, font, 0.6, 2)
    cv2.putText(frame, fps_text, (w - fw - 12, fh + 12), font, 0.6, (0, 255, 255), 2, cv2.LINE_AA)
    return frame


# ----------------------------------------------------------------------------
# 8. MAIN LOOP
# ----------------------------------------------------------------------------
def main():
    cam = CameraThread(0)                     # start background capture
    print("Starting camera...")
    # Wait (with a timeout) for the first frame so we know the device works.
    t0 = time.time()
    while cam.read() is None:
        if time.time() - t0 > 5.0:            # give up after 5 s
            print("ERROR: no frames from camera.")
            cam.stop()
            return
        time.sleep(0.01)

    # MediaPipe Pose: complexity 0 = lite (fastest); no segmentation; landmark
    # smoothing on to reduce jitter without adding our own filter.
    pose = mp_pose.Pose(
        model_complexity=0,
        enable_segmentation=False,
        smooth_landmarks=True,
        min_detection_confidence=0.5,
        min_tracking_confidence=0.5,
    )

    baseline_buffer = deque(maxlen=BASELINE_WINDOW)   # rolling mid-hip X -> center anchor
    hip_y_buffer = deque(maxlen=HIP_BUFFER_SIZE)      # rolling mid-hip Y -> jogging test

    # Debounce state: a lane change is only committed after DEBOUNCE_FRAMES in a row.
    confirmed_lane = 2                         # what we currently display (start: center)
    candidate_lane = 2                         # the lane we're tentatively switching to
    candidate_count = 0                        # consecutive frames seen in candidate_lane

    last_baseline_px = None                    # remembered for frames where hips drop out
    last_threshold_px = None
    jogging_flag = False

    # FPS / console timing (both updated once per second).
    fps = 0.0
    frame_counter = 0
    fps_timer = time.time()

    while True:
        frame = cam.read()
        if frame is None:                      # camera hiccup; just try again
            continue
        full_h, full_w = frame.shape[:2]

        # --- downscale for Pose (smaller image = faster inference) ---
        scale = PROCESS_WIDTH / full_w
        small = cv2.resize(frame, (PROCESS_WIDTH, int(full_h * scale)))
        rgb = cv2.cvtColor(small, cv2.COLOR_BGR2RGB)   # MediaPipe expects RGB

        rgb.flags.writeable = False            # marking read-only lets MediaPipe skip a copy
        results = pose.process(rgb)
        rgb.flags.writeable = True

        # Landmarks are normalized (0..1), so we can scale them straight onto the
        # FULL-resolution frame for crisp overlays.
        hip = None
        if results.pose_landmarks:
            hip = get_mid_hip(results.pose_landmarks.landmark, full_w, full_h)

        if hip is not None:
            nx, ny, px, py = hip
            baseline_buffer.append(nx)         # feed the drifting center anchor
            hip_y_buffer.append(ny)            # feed the jogging detector
            baseline_x = sum(baseline_buffer) / len(baseline_buffer)   # rolling average
            offset = nx - baseline_x           # signed lateral displacement from neutral
            raw_lane = get_lane(offset, LANE_THRESHOLD)

            # --- debounce the raw lane into confirmed_lane ---
            if raw_lane == confirmed_lane:
                candidate_lane = confirmed_lane   # back home: cancel any pending switch
                candidate_count = 0
            elif raw_lane == candidate_lane:
                candidate_count += 1              # same new lane again -> build confidence
                if candidate_count >= DEBOUNCE_FRAMES:
                    confirmed_lane = candidate_lane
                    candidate_count = 0
            else:
                candidate_lane = raw_lane         # a different new lane -> restart the count
                candidate_count = 1

            jogging_flag = is_jogging(hip_y_buffer, JOGGING_AMPLITUDE_THRESHOLD)
            # cache pixel positions so the overlay survives brief detection gaps
            last_baseline_px = baseline_x * full_w
            last_threshold_px = LANE_THRESHOLD * full_w

        # True until the baseline buffer has filled up for the first time.
        calibrating = len(baseline_buffer) < BASELINE_WINDOW

        # --- draw everything on the full-res frame ---
        # Lane highlight + boundary lines (skipped while still calibrating).
        if not calibrating and last_baseline_px is not None:
            frame = draw_overlay(frame, confirmed_lane, last_baseline_px,
                                 last_threshold_px, OVERLAY_ALPHA)

        # Mid-hip marker, colored to match the active lane (white while calibrating).
        if hip is not None:
            marker_color = (255, 255, 255) if calibrating else LANE_COLORS[confirmed_lane]
            cv2.circle(frame, (hip[2], hip[3]), 10, marker_color, -1)

        if DRAW_SKELETON and results.pose_landmarks:   # optional debug skeleton
            mp_drawing.draw_landmarks(frame, results.pose_landmarks, mp_pose.POSE_CONNECTIONS)

        frame = draw_hud(frame, confirmed_lane, jogging_flag, fps, calibrating)
        cv2.imshow(WINDOW_NAME, frame)

        # --- FPS measurement + console log, once per second ---
        frame_counter += 1
        now = time.time()
        if now - fps_timer >= 1.0:
            fps = frame_counter / (now - fps_timer)
            frame_counter = 0
            fps_timer = now
            lane_name = "CALIBRATING" if calibrating else LANE_NAMES[confirmed_lane]
            # flush=True so the line appears immediately even when stdout is piped to a file
            print(f"[{fps:.0f}fps] Lane: {lane_name} | {'jogging' if jogging_flag else 'still'}", flush=True)

        # waitKey(1) both pumps the GUI event loop and reads a key; Q quits.
        if cv2.waitKey(1) & 0xFF == ord('q'):
            break

    # --- clean shutdown ---
    cam.stop()
    pose.close()
    cv2.destroyAllWindows()


if __name__ == "__main__":
    main()
