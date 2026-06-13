import {
  FilesetResolver,
  PoseLandmarker
} from "https://cdn.jsdelivr.net/npm/@mediapipe/tasks-vision@0.10.35";

const VISION_WASM_URL = "https://cdn.jsdelivr.net/npm/@mediapipe/tasks-vision@0.10.35/wasm";
const POSE_MODEL_URL = "https://storage.googleapis.com/mediapipe-models/pose_landmarker/pose_landmarker_lite/float16/latest/pose_landmarker_lite.task";
const HUMAN_CONFIDENCE_MIN = 0.28;
const SHOULDER_VISIBILITY_MIN = 0.25;
const CORE_VISIBILITY_MIN = 0.14;
const INFERENCE_INTERVAL_MS = 25;
const LANE_HEARTBEAT_MS = 120;
const RUNNING_HEARTBEAT_MS = 75;
const RUNNING_KNEE_PROXIMITY_START = 0.62;
const RUNNING_KNEE_PROXIMITY_FULL = 0.26;
const RUNNING_LEG_MOTION_START = 0.012;
const RUNNING_LEG_MOTION_FULL = 0.09;``
const FOOT_ACCEL_LANE_THRESHOLD = 8;
const FOOT_ACCEL_LANE_REARM_THRESHOLD = 3;
const FOOT_ACCEL_RUNNING_START = 5;
const FOOT_ACCEL_RUNNING_FULL = 22;
const FOOT_ACCEL_JUMP_THRESHOLD = -11;
const FOOT_ACCEL_DUCK_THRESHOLD = 11;
const FOOT_ACCEL_VERTICAL_REARM_THRESHOLD = 4;
const GESTURE_COOLDOWN_MS = 200;
const CHEST_SHOULDER_WEIGHT = 0.65;

const LANDMARK = Object.freeze({
  leftShoulder: 11,
  rightShoulder: 12,
  leftHip: 23,
  rightHip: 24,
  leftKnee: 25,
  rightKnee: 26,
  leftAnkle: 27,
  rightAnkle: 28
});

const clamp = (value, min, max) => Math.max(min, Math.min(max, value));

export class PoseController {
  constructor(options) {
    this.video = options.video;
    this.canvas = options.canvas;
    this.send = options.send;
    this.onStatus = options.onStatus || (() => {});
    this.onMetrics = options.onMetrics || (() => {});
    this.onError = options.onError || (() => {});

    this.ctx = this.canvas.getContext("2d");
    this.poseLandmarker = null;
    this.stream = null;
    this.running = false;
    this.rafId = 0;
    this.lastInferenceAt = 0;
    this.lastStateSentAt = 0;
    this.lastLaneSentAt = 0;
    this.lastNeutralSentAt = 0;
    this.lastLane = 0;
    this.intensity = 0;
    this.previousSample = null;
    this.lastJumpAt = 0;
    this.lastDuckAt = 0;
    this.jumpArmed = true;
    this.duckArmed = true;
    this.calibration = null;
    this.calibrationStartedAt = 0;
    this.calibrationSamples = [];
    this.mirrorControls = true;
    this.enablePosePlank = false;
    this.laneBaseThresholds = { left: -LANE_EXIT_THRESHOLD, right: LANE_EXIT_THRESHOLD };
    this.laneThresholds = { left: -LANE_EXIT_THRESHOLD, right: LANE_EXIT_THRESHOLD };
    this.laneState = 0;
    this.laneDecision = "center";
    this.currentJumpOffsetThreshold = JUMP_OFFSET_THRESHOLD;
  }

  async start(options = {}) {
    this.mirrorControls = options.mirrorControls !== false;
    this.enablePosePlank = options.enablePosePlank === true;
    this.setStatus("loading model");
    this.poseLandmarker = this.poseLandmarker || await createPoseLandmarker();

    this.setStatus("opening camera");
    this.stream = await navigator.mediaDevices.getUserMedia({
      audio: false,
      video: {
        facingMode: "user",
        width: { ideal: 480 },
        height: { ideal: 360 },
        frameRate: { ideal: 60, max: 60 }
      }
    });

    this.video.srcObject = this.stream;
    await this.video.play();

    this.running = true;
    this.calibration = null;
    this.calibrationSamples = [];
    this.calibrationStartedAt = 0;
    this.previousSample = null;
    this.lastLane = 0;
    this.laneState = 0;
    this.laneDecision = "center";
    this.intensity = 0;
    this.jumpArmed = true;
    this.duckArmed = true;
    this.resetLaneAdaptation();
    this.sendNeutral(true);
    this.setStatus("find a person");
    this.loop();
  }

  stop() {
    this.running = false;
    if (this.rafId) cancelAnimationFrame(this.rafId);
    this.rafId = 0;

    if (this.stream) {
      this.stream.getTracks().forEach((track) => track.stop());
      this.stream = null;
    }

    this.video.srcObject = null;
    this.clearCanvas();
    this.sendNeutral(true);
    this.setStatus("stopped");
  }

  recalibrate() {
    this.calibration = null;
    this.calibrationSamples = [];
    this.calibrationStartedAt = 0;
    this.previousSample = null;
    this.intensity = 0;
    this.laneState = 0;
    this.laneDecision = "center";
    this.jumpArmed = true;
    this.duckArmed = true;
    this.resetLaneAdaptation();
    this.sendNeutral(true);
    this.setStatus("calibrating");
  }

  loop() {
    if (!this.running) return;

    const now = performance.now();
    if (now - this.lastInferenceAt >= INFERENCE_INTERVAL_MS && this.video.readyState >= HTMLMediaElement.HAVE_CURRENT_DATA) {
      this.lastInferenceAt = now;
      this.processFrame(now);
    }

    this.rafId = requestAnimationFrame(() => this.loop());
  }

  processFrame(now) {
    let result;
    try {
      result = this.poseLandmarker.detectForVideo(this.video, now);
    } catch (error) {
      this.onError(error);
      this.stop();
      return;
    }

    const landmarks = result?.landmarks?.[0];
    const debug = getPoseDebug(landmarks);
    const sample = landmarks ? makePoseSample(landmarks) : null;
    if (sample) {
      sample.t = now;
      this.applyMotionPrediction(sample);
      this.applyLaneFootPrediction(sample);
    }
    this.drawPose(landmarks, sample);

    if (!sample || sample.confidence < HUMAN_CONFIDENCE_MIN) {
      this.handleNoHuman(debug);
      return;
    }

    if (!this.calibration) {
      this.collectCalibration(sample, now);
      return;
    }

    this.classify(sample, now);
  }

  collectCalibration(sample, now) {
    if (!this.calibrationStartedAt) {
      this.calibrationStartedAt = now;
      this.calibrationSamples = [];
    }

    this.calibrationSamples.push(sample);
    const elapsed = now - this.calibrationStartedAt;
    const progress = clamp(elapsed / 2000, 0, 1);
    this.setStatus("calibrating");
    this.reportMetrics({ progress, lane: 0, intensity: 0, human: true });

    if (elapsed < 2000) return;

    this.calibration = {
      centerX: median(this.calibrationSamples.map((item) => item.centerX)),
      centerY: median(this.calibrationSamples.map((item) => item.centerY)),
      laneCenterX: median(this.calibrationSamples.map((item) => item.laneRawCenterX)),
      laneScale: Math.max(0.06, median(this.calibrationSamples.map((item) => item.laneScale))),
      scale: Math.max(0.06, median(this.calibrationSamples.map((item) => item.upperScale))),
      bodyScale: Math.max(0.08, median(this.calibrationSamples.map((item) => item.bodyScale))),
      shoulderWidth: Math.max(0.06, median(this.calibrationSamples.map((item) => item.shoulderWidth)))
    };
    this.previousSample = sample;
    this.setStatus("active");
  }

  classify(sample, now) {
    const controlScale = this.calibration.scale;
    const bodyScale = this.calibration.bodyScale || controlScale;
    const laneThresholds = this.updateStaticLaneThresholds(sample);
    const lane = this.resolveHybridLane(sample, laneThresholds);

    const leftKneeDrop = (sample.leftLegY - sample.leftHip.y) / bodyScale;
    const rightKneeDrop = (sample.rightLegY - sample.rightHip.y) / bodyScale;
    const kneeProximity = clamp(
      (RUNNING_KNEE_PROXIMITY_START - Math.min(leftKneeDrop, rightKneeDrop)) /
        (RUNNING_KNEE_PROXIMITY_START - RUNNING_KNEE_PROXIMITY_FULL),
      0,
      1
    );

    const legMotion = this.previousSample
      ? Math.max(
          Math.abs(sample.leftLegY - this.previousSample.leftLegY),
          Math.abs(sample.rightLegY - this.previousSample.rightLegY)
        ) / bodyScale
      : 0;
    const legMotionIntensity = clamp(
      (legMotion - RUNNING_LEG_MOTION_START) / (RUNNING_LEG_MOTION_FULL - RUNNING_LEG_MOTION_START),
      0,
      1
    );

    const shoulderBounce = this.previousSample
      ? Math.abs(sample.centerY - this.previousSample.centerY) / controlScale
      : 0;
    const shoulderBounceIntensity = clamp(
      (shoulderBounce - RUNNING_SHOULDER_BOUNCE_START) /
        (RUNNING_SHOULDER_BOUNCE_FULL - RUNNING_SHOULDER_BOUNCE_START),
      0,
      1
    );

    const rawIntensity = Math.max(kneeProximity, legMotionIntensity, shoulderBounceIntensity * 0.75);
    this.intensity = this.intensity * 0.58 + rawIntensity * 0.42;

    const verticalOffset = (sample.centerY - this.calibration.centerY) / controlScale;
    const previousVerticalOffset = this.previousSample
      ? (this.previousSample.centerY - this.calibration.centerY) / controlScale
      : verticalOffset;
    const verticalVelocity = verticalOffset - previousVerticalOffset;
    const jumpOffsetThreshold = this.duckArmed ? JUMP_OFFSET_THRESHOLD : DUCKING_JUMP_OFFSET_THRESHOLD;
    const jumpRearmOffset = this.duckArmed ? JUMP_REARM_OFFSET : DUCKING_JUMP_REARM_OFFSET;
    this.currentJumpOffsetThreshold = jumpOffsetThreshold;

    if (
      this.jumpArmed &&
      verticalOffset < jumpOffsetThreshold &&
      verticalVelocity < JUMP_VELOCITY_THRESHOLD &&
      now - this.lastJumpAt > GESTURE_COOLDOWN_MS
    ) {
      this.send("phone:jump", {});
      this.lastJumpAt = now;
      this.jumpArmed = false;
    }
    if (!this.jumpArmed && verticalOffset > jumpRearmOffset) this.jumpArmed = true;

    if (
      this.duckArmed &&
      verticalOffset > DUCK_OFFSET_THRESHOLD &&
      verticalVelocity > DUCK_VELOCITY_THRESHOLD &&
      now - this.lastDuckAt > GESTURE_COOLDOWN_MS
    ) {
      this.send("phone:duck", {});
      this.lastDuckAt = now;
      this.duckArmed = false;
    }
    if (!this.duckArmed && verticalOffset < DUCK_REARM_OFFSET) this.duckArmed = true;
    this.currentJumpOffsetThreshold = this.duckArmed ? JUMP_OFFSET_THRESHOLD : DUCKING_JUMP_OFFSET_THRESHOLD;

    if (this.enablePosePlank) {
      const torsoHeight = Math.abs(sample.shoulderY - sample.hipY) / bodyScale;
      this.sendThrottled("phone:plank_state", { active: torsoHeight < 0.42 }, now, 220, "lastPlankSentAt");
    }

    this.sendState(lane, now);
    this.reportMetrics({
      progress: 1,
      lane,
      intensity: this.intensity,
      human: true,
      confidence: sample.confidence,
      landmarks: sample.landmarkCount,
      kneeLift: kneeProximity,
      legMotion,
      shoulderBounce,
      verticalOffset,
      jumpOffsetThreshold: this.currentJumpOffsetThreshold,
      predictionX: sample.predictionOffsetX || 0,
      predictionY: sample.predictionOffsetY || 0,
      lanePredictionX: sample.lanePredictionOffsetX || 0,
      laneDecision: sample.laneDecision || this.laneDecision || "center",
      laneConfidence: sample.laneConfidence || 0,
      laneIntent: sample.laneIntent || 0,
      leftFootRegion: sample.leftFootPredictedRegion ?? 0,
      rightFootRegion: sample.rightFootPredictedRegion ?? 0,
      centerMassRegion: sample.centerMassRegion ?? 0,
      leftLaneThreshold: laneThresholds.left,
      rightLaneThreshold: laneThresholds.right
    });
    this.previousSample = sample;
    this.setStatus(this.intensity > 0.08 ? "active" : "ready");
  }

  sendState(lane, now) {
    if (lane !== this.lastLane || now - this.lastLaneSentAt >= LANE_HEARTBEAT_MS) {
      this.send("phone:lane_position", { lane });
      this.lastLane = lane;
      this.lastLaneSentAt = now;
    }

    if (now - this.lastStateSentAt >= RUNNING_HEARTBEAT_MS) {
      this.send("phone:running_state", { intensity: roundUnit(this.intensity) });
      this.lastStateSentAt = now;
    }
  }

  sendThrottled(type, data, now, interval, stampName) {
    if (!this[stampName] || now - this[stampName] >= interval) {
      this.send(type, data);
      this[stampName] = now;
    }
  }

  applyMotionPrediction(sample) {
    const previous = this.previousSample;
    if (!previous?.t || !this.calibration) return;

    const dt = clamp(sample.t - previous.t, 8, 120);
    const predictionRatio = CONTROL_PREDICTION_MS / dt;
    const maxOffset = (this.calibration.scale || sample.upperScale || 0.08) * CONTROL_PREDICTION_MAX_SCALE;
    const offsetX = clamp((sample.rawCenterX - previous.rawCenterX) * predictionRatio, -maxOffset, maxOffset);
    const offsetY = clamp((sample.rawCenterY - previous.rawCenterY) * predictionRatio, -maxOffset, maxOffset);

    sample.velocityX = (sample.rawCenterX - previous.rawCenterX) / dt;
    sample.velocityY = (sample.rawCenterY - previous.rawCenterY) / dt;
    sample.predictionOffsetX = offsetX;
    sample.predictionOffsetY = offsetY;
    sample.centerX = clamp(sample.rawCenterX + offsetX, 0, 1);
    sample.centerY = clamp(sample.rawCenterY + offsetY, 0, 1);
  }

  applyLaneFootPrediction(sample) {
    sample.laneControlX = sample.laneRawCenterX;
    sample.laneControlY = sample.laneRawCenterY;
    sample.leadFoot = "center";
    sample.laneLeadDirection = 0;
    sample.laneIntent = 0;

    const previous = this.previousSample;
    if (!previous?.t || !this.calibration) return;

    const dt = clamp(sample.t - previous.t, 8, 120);
    const leftRawVelocity = (sample.leftFootX - previous.leftFootX) / dt;
    const rightRawVelocity = (sample.rightFootX - previous.rightFootX) / dt;
    const leftMappedVelocity = this.mirrorControls ? -leftRawVelocity : leftRawVelocity;
    const rightMappedVelocity = this.mirrorControls ? -rightRawVelocity : rightRawVelocity;
    const maxOffset = (this.calibration.laneScale || this.calibration.scale || sample.laneScale) * FOOT_PREDICTION_MAX_SCALE;
    const leftOffsetX = clamp(leftRawVelocity * FOOT_PREDICTION_MS, -maxOffset, maxOffset);
    const rightOffsetX = clamp(rightRawVelocity * FOOT_PREDICTION_MS, -maxOffset, maxOffset);
    const leftPredictedX = clamp(sample.leftFootX + leftOffsetX, 0, 1);
    const rightPredictedX = clamp(sample.rightFootX + rightOffsetX, 0, 1);
    const strongestVelocity =
      Math.abs(leftMappedVelocity) >= Math.abs(rightMappedVelocity) ? leftMappedVelocity : rightMappedVelocity;
    const useLeft = Math.abs(leftMappedVelocity) >= Math.abs(rightMappedVelocity);
    const leadMappedVelocity = useLeft ? leftMappedVelocity : rightMappedVelocity;
    const leadRawVelocity = useLeft ? leftRawVelocity : rightRawVelocity;
    const leadOffsetX = useLeft ? leftOffsetX : rightOffsetX;
    const leadPredictedX = useLeft ? leftPredictedX : rightPredictedX;

    let direction = 0;
    if (Math.abs(strongestVelocity) > LEAD_FOOT_VELOCITY_DEADZONE) {
      direction = strongestVelocity < 0 ? -1 : 1;
    }

    const footRegionThresholds = this.laneBaseThresholds || this.laneThresholds;
    sample.leftFootMappedVelocityX = leftMappedVelocity;
    sample.rightFootMappedVelocityX = rightMappedVelocity;
    sample.leftFootPredictedX = leftPredictedX;
    sample.rightFootPredictedX = rightPredictedX;
    sample.leftFootRegion = this.getFootLaneRegion(sample, "left", footRegionThresholds);
    sample.rightFootRegion = this.getFootLaneRegion(sample, "right", footRegionThresholds);
    sample.leftFootPredictedRegion = this.getLaneRegionForRawX(leftPredictedX, sample, footRegionThresholds);
    sample.rightFootPredictedRegion = this.getLaneRegionForRawX(rightPredictedX, sample, footRegionThresholds);
    sample.centerMassRegion = this.getCenterMassLaneRegion(sample, footRegionThresholds);

    if (direction === 0) return;

    sample.leadFoot = useLeft ? "left" : "right";
    sample.laneLeadDirection = direction;
    sample.laneVelocityX = leadRawVelocity;
    sample.laneMappedVelocityX = leadMappedVelocity;
    sample.lanePredictionOffsetX = leadOffsetX;
    sample.laneControlX = leadPredictedX;
    sample.laneControlY = useLeft ? sample.leftFootY : sample.rightFootY;
  }

  getFootLaneRegion(sample, foot, thresholds = this.laneThresholds) {
    const rawX = foot === "left" ? sample.leftFootX : sample.rightFootX;
    return this.getLaneRegionForRawX(rawX, sample, thresholds);
  }

  getLaneRegionForRawX(rawX, sample, thresholds = this.laneThresholds) {
    const mappedDelta = this.getMappedLaneDeltaForRawX(rawX, sample);
    return this.getLaneRegionForDelta(mappedDelta, thresholds);
  }

  getMappedLaneDeltaForRawX(rawX, sample) {
    const laneCenterX = this.calibration.laneCenterX ?? this.calibration.centerX;
    const laneScale = this.calibration.laneScale || this.calibration.scale || sample.laneScale || 0.08;
    const rawDelta = (rawX - laneCenterX) / laneScale;
    return this.mirrorControls ? -rawDelta : rawDelta;
  }

  getLaneRegionForDelta(mappedDelta, thresholds = this.laneThresholds) {
    thresholds = thresholds || { left: -LANE_EXIT_THRESHOLD, right: LANE_EXIT_THRESHOLD };

    if (mappedDelta < thresholds.left) return -1;
    if (mappedDelta > thresholds.right) return 1;
    return 0;
  }

  resolveHybridLane(sample, thresholds) {
    const currentLane = this.laneState ?? this.lastLane ?? 0;
    const centerRegion = this.getCenterMassLaneRegion(sample, thresholds);
    const leftRegion = this.getLaneRegionForRawX(sample.leftFootPredictedX ?? sample.leftFootX, sample, thresholds);
    const rightRegion = this.getLaneRegionForRawX(sample.rightFootPredictedX ?? sample.rightFootX, sample, thresholds);
    const leadRegion = sample.leadFoot === "right" ? rightRegion : sample.leadFoot === "left" ? leftRegion : 0;
    const leadDirection = sample.laneLeadDirection || 0;
    const fastIntent =
      leadDirection !== 0 &&
      leadRegion === leadDirection &&
      Math.abs(sample.laneMappedVelocityX || 0) > LEAD_FOOT_VELOCITY_DEADZONE
        ? leadDirection
        : 0;
    const leftVelocitySide = this.getVelocitySide(sample.leftFootMappedVelocityX || 0);
    const rightVelocitySide = this.getVelocitySide(sample.rightFootMappedVelocityX || 0);
    const bothFeetRegion = leftRegion === rightRegion ? leftRegion : 0;
    const scores = { "-1": 0, "0": 0, "1": 0 };

    if (centerRegion !== 0) scores[centerRegion] += LANE_COM_SCORE;
    else scores[0] += LANE_COM_SCORE;

    if (bothFeetRegion !== 0) {
      scores[bothFeetRegion] += LANE_BOTH_FEET_SCORE;
    } else if (leftRegion === 0 && rightRegion === 0) {
      scores[0] += LANE_BOTH_FEET_SCORE;
    } else {
      if (leftRegion !== 0) scores[leftRegion] += LANE_ONE_FOOT_SCORE;
      else scores[0] += LANE_ONE_FOOT_SCORE * 0.55;

      if (rightRegion !== 0) scores[rightRegion] += LANE_ONE_FOOT_SCORE;
      else scores[0] += LANE_ONE_FOOT_SCORE * 0.55;
    }

    if (fastIntent !== 0) scores[fastIntent] += LANE_FAST_FOOT_SCORE;
    if (leftVelocitySide !== 0 && leftRegion === leftVelocitySide) scores[leftVelocitySide] += LANE_VELOCITY_SCORE;
    if (rightVelocitySide !== 0 && rightRegion === rightVelocitySide) scores[rightVelocitySide] += LANE_VELOCITY_SCORE;
    if (currentLane !== 0) scores[currentLane] += LANE_HOLD_SCORE;

    if (
      currentLane !== 0 &&
      centerRegion === 0 &&
      (leadDirection === -currentLane || bothFeetRegion === 0)
    ) {
      scores[0] += LANE_FAST_FOOT_SCORE;
    }

    let nextLane = currentLane;
    let decision = "hold";
    const sideCandidate = scores["-1"] > scores["1"] ? -1 : 1;
    const sideScore = scores[sideCandidate];

    if (fastIntent !== 0 && sideScore >= LANE_ENTER_SCORE) {
      nextLane = fastIntent;
      decision = "fast-foot";
    } else if (sideScore >= LANE_ENTER_SCORE && sideScore > scores["0"] + 0.12) {
      nextLane = sideCandidate;
      decision = centerRegion === sideCandidate ? "confirmed" : "feet";
    } else if (scores["0"] >= LANE_CENTER_SCORE && scores["0"] >= sideScore) {
      nextLane = 0;
      decision = centerRegion === 0 ? "center-com" : "center-feet";
    }

    this.laneState = nextLane;
    this.laneDecision = decision;
    sample.leftFootPredictedRegion = leftRegion;
    sample.rightFootPredictedRegion = rightRegion;
    sample.centerMassRegion = centerRegion;
    sample.laneIntent = fastIntent || (sideScore > scores["0"] ? sideCandidate : 0);
    sample.laneConfidence = clamp(Math.max(scores["-1"], scores["0"], scores["1"]) / 3, 0, 1);
    sample.laneDecision = decision;
    return nextLane;
  }

  getVelocitySide(mappedVelocity) {
    if (Math.abs(mappedVelocity) <= LEAD_FOOT_VELOCITY_DEADZONE) return 0;
    return mappedVelocity < 0 ? -1 : 1;
  }

  updateStaticLaneThresholds(sample = null) {
    this.laneBaseThresholds = { left: -LANE_EXIT_THRESHOLD, right: LANE_EXIT_THRESHOLD };
    this.laneThresholds = this.laneBaseThresholds;

    if (sample && this.calibration) {
      sample.centerMassRegion = this.getCenterMassLaneRegion(sample, this.laneThresholds);
    }

    return this.laneThresholds;
  }

  getCenterMassLaneRegion(sample, thresholds = this.laneThresholds) {
    const laneCenterX = this.calibration.laneCenterX ?? this.calibration.centerX;
    const laneScale = this.calibration.laneScale || this.calibration.scale || sample.laneScale || 0.08;
    const rawDelta = (sample.centerX - laneCenterX) / laneScale;
    const mappedDelta = this.mirrorControls ? -rawDelta : rawDelta;
    thresholds = thresholds || { left: -LANE_EXIT_THRESHOLD, right: LANE_EXIT_THRESHOLD };

    if (mappedDelta < thresholds.left) return -1;
    if (mappedDelta > thresholds.right) return 1;
    return 0;
  }

  resetLaneAdaptation() {
    this.laneBaseThresholds = { left: -LANE_EXIT_THRESHOLD, right: LANE_EXIT_THRESHOLD };
    this.laneThresholds = { left: -LANE_EXIT_THRESHOLD, right: LANE_EXIT_THRESHOLD };
    this.laneState = 0;
    this.laneDecision = "center";
    this.currentJumpOffsetThreshold = JUMP_OFFSET_THRESHOLD;
  }

  handleNoHuman(debug = {}) {
    this.previousSample = null;
    this.intensity = 0;
    this.jumpArmed = true;
    this.duckArmed = true;
    this.resetLaneAdaptation();
    if (!this.calibration) {
      this.calibrationStartedAt = 0;
      this.calibrationSamples = [];
    }
    this.sendNeutral();
    this.setStatus("no human");
    this.reportMetrics({
      progress: this.calibration ? 1 : 0,
      lane: 0,
      intensity: 0,
      human: false,
      confidence: debug.confidence || 0,
      landmarks: debug.landmarkCount || 0,
      hint: debug.hint || "face the camera"
    });
  }

  sendNeutral(force = false) {
    const now = performance.now();
    if (!force && now - this.lastNeutralSentAt < 250) return;
    this.lastNeutralSentAt = now;
    this.send("phone:running_state", { intensity: 0 });
    this.send("phone:lane_position", { lane: 0 });
    if (this.enablePosePlank) this.send("phone:plank_state", { active: false });
    this.lastLane = 0;
  }

  setStatus(status) {
    this.onStatus(status);
  }

  reportMetrics(metrics) {
    this.onMetrics(metrics);
  }

  drawPose(landmarks, sample) {
    const width = this.video.videoWidth || this.video.clientWidth || 640;
    const height = this.video.videoHeight || this.video.clientHeight || 480;
    if (this.canvas.width !== width) this.canvas.width = width;
    if (this.canvas.height !== height) this.canvas.height = height;

    this.ctx.clearRect(0, 0, width, height);
    this.drawLaneRegions(width, height);
    this.drawVerticalMotionRegions(width, height);

    if (!landmarks) return;

    this.ctx.save();
    this.ctx.lineWidth = Math.max(2, width * 0.006);
    this.ctx.strokeStyle = "rgba(64, 210, 173, 0.9)";
    this.ctx.fillStyle = "rgba(255, 230, 112, 0.96)";
    drawLink(this.ctx, landmarks, LANDMARK.leftShoulder, LANDMARK.rightShoulder, width, height);
    drawLink(this.ctx, landmarks, LANDMARK.leftShoulder, LANDMARK.leftHip, width, height);
    drawLink(this.ctx, landmarks, LANDMARK.rightShoulder, LANDMARK.rightHip, width, height);
    drawLink(this.ctx, landmarks, LANDMARK.leftHip, LANDMARK.rightHip, width, height);
    drawLink(this.ctx, landmarks, LANDMARK.leftHip, LANDMARK.leftKnee, width, height);
    drawLink(this.ctx, landmarks, LANDMARK.rightHip, LANDMARK.rightKnee, width, height);
    drawLink(this.ctx, landmarks, LANDMARK.leftKnee, LANDMARK.leftAnkle, width, height);
    drawLink(this.ctx, landmarks, LANDMARK.rightKnee, LANDMARK.rightAnkle, width, height);

    Object.values(LANDMARK).forEach((index) => {
      const point = landmarks[index];
      if (!point || point.visibility < 0.35) return;
      this.ctx.beginPath();
      this.ctx.arc(point.x * width, point.y * height, Math.max(3, width * 0.008), 0, Math.PI * 2);
      this.ctx.fill();
    });

    this.drawControlPoint(sample, width, height);
    this.drawLaneFootPoint(sample, width, height);
    this.ctx.restore();
  }

  drawLaneRegions(width, height) {
    if (!this.calibration) return;

    const thresholds = this.laneThresholds || { left: -LANE_EXIT_THRESHOLD, right: LANE_EXIT_THRESHOLD };
    const leftRawThreshold = this.mirrorControls ? -thresholds.right : thresholds.left;
    const rightRawThreshold = this.mirrorControls ? -thresholds.left : thresholds.right;
    const laneCenterX = this.calibration.laneCenterX ?? this.calibration.centerX;
    const laneScale = this.calibration.laneScale || this.calibration.scale;
    const rawA = (laneCenterX + leftRawThreshold * laneScale) * width;
    const rawB = (laneCenterX + rightRawThreshold * laneScale) * width;
    const leftBoundary = clamp(Math.min(rawA, rawB), 0, width);
    const rightBoundary = clamp(Math.max(rawA, rawB), 0, width);

    this.ctx.save();
    this.ctx.fillStyle = this.mirrorControls ? "rgba(255, 100, 112, 0.12)" : "rgba(88, 166, 255, 0.12)";
    this.ctx.fillRect(0, 0, leftBoundary, height);
    this.ctx.fillStyle = "rgba(69, 196, 143, 0.14)";
    this.ctx.fillRect(leftBoundary, 0, Math.max(0, rightBoundary - leftBoundary), height);
    this.ctx.fillStyle = this.mirrorControls ? "rgba(88, 166, 255, 0.12)" : "rgba(255, 100, 112, 0.12)";
    this.ctx.fillRect(rightBoundary, 0, Math.max(0, width - rightBoundary), height);

    this.ctx.lineWidth = Math.max(2, width * 0.004);
    this.ctx.strokeStyle = "rgba(255, 255, 255, 0.55)";
    drawVerticalLine(this.ctx, leftBoundary, height);
    drawVerticalLine(this.ctx, rightBoundary, height);

    this.ctx.strokeStyle = "rgba(255, 209, 102, 0.9)";
    drawVerticalLine(this.ctx, laneCenterX * width, height);
    this.ctx.restore();
  }

  drawVerticalMotionRegions(width, height) {
    if (!this.calibration) return;

    const centerY = clamp(this.calibration.centerY * height, 0, height);
    const jumpOffsetThreshold = this.currentJumpOffsetThreshold ?? JUMP_OFFSET_THRESHOLD;
    const jumpBoundary = clamp(
      (this.calibration.centerY + jumpOffsetThreshold * this.calibration.scale) * height,
      0,
      height
    );
    const duckBoundary = clamp(
      (this.calibration.centerY + DUCK_OFFSET_THRESHOLD * this.calibration.scale) * height,
      0,
      height
    );

    this.ctx.save();
    this.ctx.fillStyle = "rgba(88, 166, 255, 0.11)";
    this.ctx.fillRect(0, 0, width, jumpBoundary);
    this.ctx.fillStyle = "rgba(210, 168, 255, 0.11)";
    this.ctx.fillRect(0, duckBoundary, width, Math.max(0, height - duckBoundary));

    this.ctx.lineWidth = Math.max(2, width * 0.004);
    this.ctx.strokeStyle = "rgba(88, 166, 255, 0.78)";
    drawHorizontalLine(this.ctx, jumpBoundary, width);
    this.ctx.strokeStyle = "rgba(210, 168, 255, 0.78)";
    drawHorizontalLine(this.ctx, duckBoundary, width);
    this.ctx.strokeStyle = "rgba(255, 209, 102, 0.9)";
    drawHorizontalLine(this.ctx, centerY, width);
    this.ctx.restore();
  }

  drawControlPoint(sample, width, height) {
    if (!sample) return;

    const x = sample.centerX * width;
    const y = sample.centerY * height;
    const radius = Math.max(7, width * 0.018);

    this.ctx.save();
    this.ctx.lineWidth = Math.max(2, width * 0.006);
    this.ctx.fillStyle = "rgba(255, 68, 68, 0.95)";
    this.ctx.strokeStyle = "rgba(255, 255, 255, 0.95)";
    this.ctx.beginPath();
    this.ctx.arc(x, y, radius, 0, Math.PI * 2);
    this.ctx.fill();
    this.ctx.stroke();

    this.ctx.strokeStyle = "rgba(255, 255, 255, 0.85)";
    this.ctx.beginPath();
    this.ctx.moveTo(x - radius * 1.6, y);
    this.ctx.lineTo(x + radius * 1.6, y);
    this.ctx.moveTo(x, y - radius * 1.6);
    this.ctx.lineTo(x, y + radius * 1.6);
    this.ctx.stroke();
    this.ctx.restore();
  }

  drawLaneFootPoint(sample, width, height) {
    if (!sample || !Number.isFinite(sample.laneControlX) || !Number.isFinite(sample.laneControlY)) return;

    const x = sample.laneControlX * width;
    const y = sample.laneControlY * height;
    const radius = Math.max(6, width * 0.016);

    this.ctx.save();
    this.ctx.lineWidth = Math.max(2, width * 0.005);
    this.ctx.fillStyle = "rgba(255, 166, 77, 0.95)";
    this.ctx.strokeStyle = "rgba(0, 0, 0, 0.72)";
    this.ctx.beginPath();
    this.ctx.arc(x, y, radius, 0, Math.PI * 2);
    this.ctx.fill();
    this.ctx.stroke();

    if (sample.lanePredictionOffsetX) {
      const rawX = (sample.leadFoot === "left" ? sample.leftFootX : sample.rightFootX) * width;
      const rawY = (sample.leadFoot === "left" ? sample.leftFootY : sample.rightFootY) * height;
      this.ctx.strokeStyle = "rgba(255, 166, 77, 0.9)";
      this.ctx.beginPath();
      this.ctx.moveTo(rawX, rawY);
      this.ctx.lineTo(x, y);
      this.ctx.stroke();
    }
    this.ctx.restore();
  }

  clearCanvas() {
    this.ctx.clearRect(0, 0, this.canvas.width, this.canvas.height);
  }
}

async function createPoseLandmarker() {
  const vision = await FilesetResolver.forVisionTasks(VISION_WASM_URL);
  const baseOptions = { modelAssetPath: POSE_MODEL_URL };
  try {
    return await PoseLandmarker.createFromOptions(vision, {
      baseOptions: { ...baseOptions, delegate: "GPU" },
      runningMode: "VIDEO",
      numPoses: 1,
      minPoseDetectionConfidence: 0.35,
      minPosePresenceConfidence: 0.35,
      minTrackingConfidence: 0.35
    });
  } catch {
    return await PoseLandmarker.createFromOptions(vision, {
      baseOptions,
      runningMode: "VIDEO",
      numPoses: 1,
      minPoseDetectionConfidence: 0.35,
      minPosePresenceConfidence: 0.35,
      minTrackingConfidence: 0.35
    });
  }
}

function makePoseSample(landmarks) {
  const leftShoulder = landmarks[LANDMARK.leftShoulder];
  const rightShoulder = landmarks[LANDMARK.rightShoulder];
  const leftHip = landmarks[LANDMARK.leftHip];
  const rightHip = landmarks[LANDMARK.rightHip];
  const leftKnee = landmarks[LANDMARK.leftKnee];
  const rightKnee = landmarks[LANDMARK.rightKnee];
  const leftAnkle = landmarks[LANDMARK.leftAnkle];
  const rightAnkle = landmarks[LANDMARK.rightAnkle];
  const core = [leftShoulder, rightShoulder, leftHip, rightHip];

  if (!isVisible(leftShoulder, SHOULDER_VISIBILITY_MIN) || !isVisible(rightShoulder, SHOULDER_VISIBILITY_MIN)) return null;
  if (!hasFinitePoint(leftHip) || !hasFinitePoint(rightHip)) return null;

  const shoulderX = average(leftShoulder.x, rightShoulder.x);
  const shoulderY = average(leftShoulder.y, rightShoulder.y);
  const hipX = average(leftHip.x, rightHip.x);
  const hipY = average(leftHip.y, rightHip.y);
  const shoulderWidth = distance(leftShoulder, rightShoulder);
  const hipWidth = distance(leftHip, rightHip);
  const torsoHeight = Math.abs(shoulderY - hipY);
  const upperScale = Math.max(shoulderWidth, 0.06);
  const bodyScale = Math.max(shoulderWidth, hipWidth, torsoHeight, 0.08);
  const chestX = weightedAverage(shoulderX, hipX, CHEST_SHOULDER_WEIGHT);
  const chestY = weightedAverage(shoulderY, hipY, CHEST_SHOULDER_WEIGHT);
  const leftLeg = pickLegPoint(leftKnee, leftAnkle, leftHip);
  const rightLeg = pickLegPoint(rightKnee, rightAnkle, rightHip);
  const leftFoot = pickFootPoint(leftAnkle, leftKnee, leftHip);
  const rightFoot = pickFootPoint(rightAnkle, rightKnee, rightHip);
  const laneRawCenterX = average(leftFoot.x, rightFoot.x);
  const laneRawCenterY = average(leftFoot.y, rightFoot.y);
  const laneScale = Math.max(shoulderWidth, distance(leftFoot, rightFoot), 0.06);

  return {
    centerX: chestX,
    centerY: chestY,
    rawCenterX: chestX,
    rawCenterY: chestY,
    shoulderX,
    shoulderY,
    hipX,
    hipY,
    shoulderWidth,
    upperScale,
    bodyScale,
    laneScale,
    laneRawCenterX,
    laneRawCenterY,
    leftHip,
    rightHip,
    leftKnee: leftLeg,
    rightKnee: rightLeg,
    leftLegY: leftLeg.y,
    rightLegY: rightLeg.y,
    leftFootX: leftFoot.x,
    leftFootY: leftFoot.y,
    rightFootX: rightFoot.x,
    rightFootY: rightFoot.y,
    confidence: core.reduce((sum, item) => sum + scorePoint(item), 0) / core.length,
    landmarkCount: landmarks.filter(hasFinitePoint).length
  };
}

function getPoseDebug(landmarks) {
  if (!landmarks?.length) {
    return { confidence: 0, landmarkCount: 0, hint: "no pose landmarks" };
  }

  const leftShoulder = landmarks[LANDMARK.leftShoulder];
  const rightShoulder = landmarks[LANDMARK.rightShoulder];
  const leftHip = landmarks[LANDMARK.leftHip];
  const rightHip = landmarks[LANDMARK.rightHip];
  const shoulderConfidence = average(scorePoint(leftShoulder), scorePoint(rightShoulder));
  const hipConfidence = average(scorePoint(leftHip), scorePoint(rightHip));
  const confidence = average(shoulderConfidence, hipConfidence);

  let hint = "hold still facing camera";
  if (shoulderConfidence < SHOULDER_VISIBILITY_MIN) {
    hint = "bring shoulders into frame";
  } else if (hipConfidence < CORE_VISIBILITY_MIN) {
    hint = "step back until hips are visible";
  }

  return {
    confidence,
    landmarkCount: landmarks.filter(hasFinitePoint).length,
    hint
  };
}

function pickLegPoint(knee, ankle, fallback) {
  if (isVisible(knee, CORE_VISIBILITY_MIN)) return knee;
  if (isVisible(ankle, CORE_VISIBILITY_MIN)) return ankle;
  return fallback;
}

function pickFootPoint(ankle, knee, fallback) {
  if (isVisible(ankle, CORE_VISIBILITY_MIN)) return ankle;
  if (isVisible(knee, CORE_VISIBILITY_MIN)) return knee;
  return fallback;
}

function drawLink(ctx, landmarks, from, to, width, height) {
  const a = landmarks[from];
  const b = landmarks[to];
  if (!isVisible(a, 0.35) || !isVisible(b, 0.35)) return;
  ctx.beginPath();
  ctx.moveTo(a.x * width, a.y * height);
  ctx.lineTo(b.x * width, b.y * height);
  ctx.stroke();
}

function drawVerticalLine(ctx, x, height) {
  ctx.beginPath();
  ctx.moveTo(x, 0);
  ctx.lineTo(x, height);
  ctx.stroke();
}

function drawHorizontalLine(ctx, y, width) {
  ctx.beginPath();
  ctx.moveTo(0, y);
  ctx.lineTo(width, y);
  ctx.stroke();
}

function isVisible(point, threshold = 0.45) {
  if (!point) return false;
  return scorePoint(point) >= threshold && hasFinitePoint(point);
}

function hasFinitePoint(point) {
  return !!point && Number.isFinite(point.x) && Number.isFinite(point.y);
}

function scorePoint(point) {
  if (!point) return 0;
  return point.visibility ?? point.presence ?? 1;
}

function average(a, b) {
  return (a + b) / 2;
}

function weightedAverage(primary, secondary, primaryWeight) {
  return primary * primaryWeight + secondary * (1 - primaryWeight);
}

function distance(a, b) {
  return Math.hypot(a.x - b.x, a.y - b.y);
}

function median(values) {
  const sorted = values.filter(Number.isFinite).sort((a, b) => a - b);
  if (!sorted.length) return 0;
  return sorted[Math.floor(sorted.length / 2)];
}

function roundUnit(value) {
  return Math.round(clamp(value, 0, 1) * 100) / 100;
}
