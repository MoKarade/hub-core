"""Service CLIP : embeddings image+text pour recherche semantique photos.

Lazy-loaded : le modele (~360 MB pour ViT-B-32) n'est charge que si on
appelle vraiment un endpoint CLIP. Permet a hub-core de demarrer meme si
torch/open_clip ne sont pas installes (Marc fait `pip install -e .[ml]`
quand il veut activer cette feature).

Performance approx (CPU only, ViT-B-32) :
- 1 image (224x224) : ~80ms
- 1 query texte : ~30ms
- 1000 photos sequentiel : ~80s. Pour batch 32 : ~30s.

Marc a une RTX 5080 mais Ollama l'occupe deja. CLIP sur GPU = encore plus
rapide (~5x), mais memoire partagee complique. Pour l'instant CPU.
"""

from __future__ import annotations

import io
import logging
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING

import httpx

if TYPE_CHECKING:
    import numpy as np

logger = logging.getLogger(__name__)

# Model par defaut : ViT-B-32 (open_clip), ~360 MB, embedding 512-d
DEFAULT_MODEL = "ViT-B-32"
DEFAULT_PRETRAINED = "laion2b_s34b_b79k"  # bon equilibre qualite/taille


class ClipNotInstalledError(RuntimeError):
    """Levee quand torch/open_clip ne sont pas dispos.

    Solution : `cd hub-core && pip install -e .[ml]`.
    """


@lru_cache(maxsize=1)
def _load_model() -> tuple:
    """Charge le modele CLIP + tokenizer + preprocess. Cache 1 fois."""
    try:
        import open_clip
        import torch
    except ImportError as e:
        raise ClipNotInstalledError(
            "open_clip / torch non installes. Lance "
            "`cd hub-core && pip install -e .[ml]` puis restart."
        ) from e

    logger.info("Loading CLIP model %s/%s ...", DEFAULT_MODEL, DEFAULT_PRETRAINED)
    model, _, preprocess = open_clip.create_model_and_transforms(
        DEFAULT_MODEL, pretrained=DEFAULT_PRETRAINED
    )
    tokenizer = open_clip.get_tokenizer(DEFAULT_MODEL)
    model.eval()
    device = "cuda" if torch.cuda.is_available() else "cpu"
    model = model.to(device)
    logger.info("CLIP loaded on %s", device)
    return model, preprocess, tokenizer, device


def encode_image_bytes(image_bytes: bytes) -> np.ndarray:
    """Encode 1 image (bytes) en vecteur 512-d. Normalise (cosine-ready)."""
    import numpy as np
    import torch
    from PIL import Image

    model, preprocess, _, device = _load_model()
    img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
    tensor = preprocess(img).unsqueeze(0).to(device)
    with torch.no_grad():
        emb = model.encode_image(tensor)
        emb = emb / emb.norm(dim=-1, keepdim=True)  # L2 normalize
    return emb.cpu().numpy()[0].astype(np.float32)


def encode_image_path(path: Path) -> np.ndarray:
    """Encode 1 image depuis le disque."""
    return encode_image_bytes(path.read_bytes())


async def encode_image_url(url: str) -> np.ndarray:
    """Encode 1 image telechargee depuis URL (Google Photos baseUrl, etc.)."""
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.get(url)
        r.raise_for_status()
        return encode_image_bytes(r.content)


def encode_text(query: str) -> np.ndarray:
    """Encode 1 query texte en vecteur 512-d. Normalise."""
    import numpy as np
    import torch

    model, _, tokenizer, device = _load_model()
    tokens = tokenizer([query]).to(device)
    with torch.no_grad():
        emb = model.encode_text(tokens)
        emb = emb / emb.norm(dim=-1, keepdim=True)
    return emb.cpu().numpy()[0].astype(np.float32)


def cosine_sim_batch(query_vec: np.ndarray, db_vecs: np.ndarray) -> np.ndarray:
    """Cosine similarity entre 1 query et N db vecteurs. Tous deja normalises.

    Retourne un vecteur (N,) de scores entre -1 et 1.
    """
    import numpy as np

    return np.dot(db_vecs, query_vec).astype(np.float32)
