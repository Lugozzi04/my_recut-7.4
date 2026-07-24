from typing import Tuple


def map_intensity(intensity_0_100: int) -> Tuple[float, float, float]:
    """
    Unici 2 parametri UI: Intensity + Threshold.
    Intensity qui controlla tutto il resto.
    Returns: (min_silence, edge_keep, min_keep)
    """
    x = max(0, min(100, intensity_0_100)) / 100.0

    # Gameplay+commento: Natural taglia solo dead air, Super taglia aggressivo.
    min_silence = (0.80 * (1 - x)) + (0.16 * x)  # s
    edge_keep   = (0.22 * (1 - x)) + (0.05 * x)  # s
    min_keep    = (0.25 * (1 - x)) + (0.10 * x)  # s
    return float(min_silence), float(edge_keep), float(min_keep)
