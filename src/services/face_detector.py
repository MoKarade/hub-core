"""Service face recognition (dlib via face_recognition library).

Lazy-loaded : dlib n'est pas requis pour faire tourner hub-core. Marc fait
`pip install -e .[ml]` quand il active la feature.

Workflow :
1. detect_and_encode(image_bytes) -> [{bbox, encoding}, ...] (1 par visage)
2. cluster_encodings(encodings_list) -> labels via DBSCAN cosine

Performance approx (CPU) :
- 1 photo HOG (default) : ~150ms
- 1 photo CNN (plus precis) : ~2s
- 1000 photos HOG : ~2.5min
"""

from __future__ import annotations

import io
import logging
from functools import lru_cache
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)


class FaceRecognitionNotInstalledError(RuntimeError):
    """Levee si face_recognition / dlib pas installe.

    Solution : `cd hub-core && pip install -e .[ml]`. Sur Windows il faut
    aussi le SDK MSVC pour compiler dlib (visualstudio.microsoft.com -> Build Tools).
    """


@lru_cache(maxsize=1)
def _ensure_loaded() -> bool:
    """Charge face_recognition + numpy. Cache 1 fois."""
    try:
        import face_recognition  # noqa: F401
        import numpy  # noqa: F401
    except ImportError as e:
        raise FaceRecognitionNotInstalledError(
            "face_recognition / dlib non installes. Lance "
            "`cd hub-core && pip install -e .[ml]` puis restart. "
            "Sur Windows : installer aussi MSVC Build Tools."
        ) from e
    return True


def detect_and_encode(
    image_bytes: bytes,
    model: str = "hog",
) -> list[dict[str, Any]]:
    """Detecte les visages dans 1 image, retourne [{bbox, encoding}, ...].

    bbox = (top, right, bottom, left)
    encoding = vecteur 128-d normalise

    model = 'hog' (rapide, CPU) ou 'cnn' (plus precis, GPU recommande).
    """
    _ensure_loaded()
    import face_recognition
    import numpy as np
    from PIL import Image

    img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
    arr = np.array(img)

    bboxes = face_recognition.face_locations(arr, model=model)
    if not bboxes:
        return []

    encodings = face_recognition.face_encodings(arr, known_face_locations=bboxes)
    out = []
    for bbox, enc in zip(bboxes, encodings, strict=False):
        out.append(
            {
                "bbox": list(bbox),  # (top, right, bottom, left)
                "encoding": enc.tolist(),
            }
        )
    return out


def cluster_encodings(
    encodings: list[list[float]],
    eps: float = 0.5,
    min_samples: int = 2,
) -> list[int]:
    """DBSCAN sur N encodings 128-d. Retourne liste de labels (-1 = bruit).

    eps cosine ~0.5 = bonne separation pour dlib face encodings.
    min_samples=2 = un cluster requiert au moins 2 visages similaires.
    """
    _ensure_loaded()
    try:
        from sklearn.cluster import DBSCAN
    except ImportError as e:
        raise FaceRecognitionNotInstalledError(
            "scikit-learn non installe (clustering DBSCAN). Inclus dans .[ml] extras."
        ) from e

    import numpy as np

    if not encodings:
        return []

    X = np.array(encodings)
    # face_recognition utilise euclidean (les encodings ne sont pas L2-norm
    # par defaut). Distance Euclidean ~0.5 = meme personne typique.
    db = DBSCAN(eps=eps, min_samples=min_samples, metric="euclidean")
    labels = db.fit_predict(X)
    return labels.tolist()


async def encode_image_url(url: str, model: str = "hog") -> list[dict[str, Any]]:
    """Telecharge image + detect+encode."""
    import httpx

    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.get(url)
        r.raise_for_status()
        return detect_and_encode(r.content, model=model)
