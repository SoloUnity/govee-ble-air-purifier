"""Pure Custom Auto speed policy."""

CUSTOM_AUTO_SPEEDS = (20, 40, 60, 80, 100)
UPSHIFT_CONFIRMATION_SAMPLES = 2
SPEED_TO_MODE = {
    20: "Sleep",
    40: "Low",
    60: "Medium",
    80: "High",
    100: "Turbo",
}
MODE_TO_SPEED = {mode: speed for speed, mode in SPEED_TO_MODE.items()}


def speed_for_pm(pm25: int, up_thresholds: tuple[int, int, int, int]) -> int:
    """Return the speed required by a PM2.5 reading and upward thresholds."""

    speed = CUSTOM_AUTO_SPEEDS[0]
    for threshold, candidate in zip(
        up_thresholds, CUSTOM_AUTO_SPEEDS[1:], strict=True
    ):
        if pm25 > threshold:
            speed = candidate
    return speed
