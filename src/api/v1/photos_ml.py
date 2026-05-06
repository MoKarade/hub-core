"""Endpoints /v1/photos/* ML : recherche semantique CLIP + reconnaissance faciale.

Phase 7+. Endpoints separes de photos.py pour clarite.

Setup ML :
- `cd hub-core && pip install -e .[ml]` (pulls torch, open_clip, face_recognition, sklearn)
- Sur Windows : MSVC Build Tools requis pour compiler dlib
- Premier appel charge ~360 MB (CLIP) + ~100 MB (dlib) en RAM. Puis cache.

Workflow CLIP :
1. POST /photos/embed?limit=100 → process N photos sans embedding
2. GET /photos/search?q=...&top_k=20 → semantic search (text → photos similaires)

Workflow faces :
1. POST /photos/detect-faces?limit=100 → detect+encode faces dans N photos
2. POST /photos/cluster-faces → DBSCAN sur tous les encodings, assigne cluster_id
3. GET /photos/face-clusters → liste des clusters avec count + sample
4. PATCH /photos/face-clusters/{id} → renomme un cluster (Marc)
5. GET /photos/by-face/{cluster_id} → photo IDs ou ce cluster apparait
"""

from __future__ import annotations

import logging
import time
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.models import FaceCluster, Photo, PhotoEmbedding, PhotoFace
from src.db.session import get_db

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/photos", tags=["photos-ml"])


# ─────────────────────────────────────────────────────────────────────────
# Schemas
# ─────────────────────────────────────────────────────────────────────────


class EmbedRequest(BaseModel):
    limit: int = Field(default=100, ge=1, le=2000)
    """Nombre de photos sans embedding a traiter dans cet appel."""

    model: str = Field(default="ViT-B-32")


class EmbedResponse(BaseModel):
    embedded: int
    skipped_no_url: int
    errors: int
    total_remaining: int
    duration_seconds: float


class SearchHit(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    photo_id: UUID
    media_id: str
    filename: str | None
    score: float


class FaceDetectRequest(BaseModel):
    limit: int = Field(default=50, ge=1, le=500)
    detection_model: str = Field(default="hog", pattern="^(hog|cnn)$")


class FaceDetectResponse(BaseModel):
    photos_processed: int
    faces_found: int
    errors: int
    duration_seconds: float


class FaceClusterOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    name: str | None
    photo_count: int
    sample_face_id: UUID | None


class FaceClusterRename(BaseModel):
    name: str | None = Field(None, max_length=100)


class ClusterRunResponse(BaseModel):
    total_faces: int
    clusters_found: int
    noise_faces: int  # cluster_id stays None (DBSCAN -1)
    duration_seconds: float


# ─────────────────────────────────────────────────────────────────────────
# CLIP : embedding + search
# ─────────────────────────────────────────────────────────────────────────


@router.post("/embed", response_model=EmbedResponse)
async def embed_photos(
    payload: EmbedRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> EmbedResponse:
    """Traite N photos sans embedding : telecharge thumbnail + encode CLIP + insert."""
    try:
        from src.services.clip_embedder import (
            ClipNotInstalledError,
            encode_image_url,
        )
    except ImportError:
        raise HTTPException(  # noqa: B904
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "Module CLIP indispo. Lance `pip install -e .[ml]` puis restart.",
        )

    start = time.monotonic()

    # Photos sans embedding (left outer join)
    stmt = (
        select(Photo)
        .outerjoin(PhotoEmbedding, PhotoEmbedding.photo_id == Photo.id)
        .where(PhotoEmbedding.id.is_(None))
        .where(Photo.base_url.is_not(None))
        .limit(payload.limit)
    )
    photos = (await db.execute(stmt)).scalars().all()

    embedded = 0
    skipped = 0
    errors = 0

    for photo in photos:
        # Photos: base_url Google a TTL ~1h, on prend la thumb 256x256
        url = (photo.base_url or "") + "=w256-h256"
        if not photo.base_url:
            skipped += 1
            continue
        try:
            vec = await encode_image_url(url)
        except ClipNotInstalledError:
            raise HTTPException(  # noqa: B904
                status.HTTP_503_SERVICE_UNAVAILABLE,
                "CLIP non installe (pip install -e .[ml])",
            )
        except Exception as e:
            logger.warning("photo_embed_failed photo=%s err=%r", photo.id, e)
            errors += 1
            continue

        db.add(
            PhotoEmbedding(
                photo_id=photo.id,
                model_name=payload.model,
                embedding=vec.tolist(),
            )
        )
        embedded += 1

    await db.commit()

    # Compte ce qui reste a faire
    total_remaining = (
        await db.execute(
            select(func.count())
            .select_from(Photo)
            .outerjoin(PhotoEmbedding, PhotoEmbedding.photo_id == Photo.id)
            .where(PhotoEmbedding.id.is_(None))
        )
    ).scalar_one() or 0

    return EmbedResponse(
        embedded=embedded,
        skipped_no_url=skipped,
        errors=errors,
        total_remaining=int(total_remaining) - embedded,
        duration_seconds=round(time.monotonic() - start, 2),
    )


@router.get("/search", response_model=list[SearchHit])
async def semantic_search(
    db: Annotated[AsyncSession, Depends(get_db)],
    q: str,
    top_k: int = 20,
    min_score: float = 0.18,
) -> list[SearchHit]:
    """Recherche semantique texte → photos similaires (cosine).

    Exemple : q="plage coucher de soleil" → top photos ressemblant.
    """
    try:
        from src.services.clip_embedder import (
            ClipNotInstalledError,
            cosine_sim_batch,
            encode_text,
        )
    except ImportError:
        raise HTTPException(  # noqa: B904
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "Module CLIP indispo. Lance `pip install -e .[ml]`.",
        )

    if not q.strip():
        return []

    try:
        query_vec = encode_text(q)
    except ClipNotInstalledError:
        raise HTTPException(  # noqa: B904
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "CLIP non installe (pip install -e .[ml])",
        )

    # Charge tous les embeddings (pour <10k photos, OK en memoire)
    stmt = select(
        PhotoEmbedding.photo_id, PhotoEmbedding.embedding, Photo.media_id, Photo.filename
    ).join(Photo, Photo.id == PhotoEmbedding.photo_id)
    rows = (await db.execute(stmt)).all()
    if not rows:
        return []

    import numpy as np

    photo_ids = [r[0] for r in rows]
    media_ids = [r[2] for r in rows]
    filenames = [r[3] for r in rows]
    db_vecs = np.array([r[1] for r in rows], dtype=np.float32)

    scores = cosine_sim_batch(query_vec, db_vecs)
    # top-K indices
    top_idx = np.argsort(-scores)[:top_k]
    return [
        SearchHit(
            photo_id=photo_ids[i],
            media_id=media_ids[i],
            filename=filenames[i],
            score=float(scores[i]),
        )
        for i in top_idx
        if scores[i] >= min_score
    ]


# ─────────────────────────────────────────────────────────────────────────
# Face detection + clustering
# ─────────────────────────────────────────────────────────────────────────


@router.post("/detect-faces", response_model=FaceDetectResponse)
async def detect_faces(
    payload: FaceDetectRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> FaceDetectResponse:
    """Detecte+encode visages dans N photos qui n'ont pas encore ete traitees.

    Idempotent : on skip les photos qui ont deja >=1 PhotoFace.
    """
    try:
        from src.services.face_detector import (
            FaceRecognitionNotInstalledError,
            encode_image_url,
        )
    except ImportError:
        raise HTTPException(  # noqa: B904
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "Module face_recognition indispo. Lance `pip install -e .[ml]`.",
        )

    start = time.monotonic()

    # Photos sans aucun PhotoFace deja traite
    stmt = (
        select(Photo)
        .outerjoin(PhotoFace, PhotoFace.photo_id == Photo.id)
        .where(PhotoFace.id.is_(None))
        .where(Photo.base_url.is_not(None))
        .limit(payload.limit)
    )
    photos = (await db.execute(stmt)).scalars().all()

    photos_processed = 0
    faces_found = 0
    errors = 0

    for photo in photos:
        url = (photo.base_url or "") + "=w800-h800"
        try:
            faces = await encode_image_url(url, model=payload.detection_model)
        except FaceRecognitionNotInstalledError:
            raise HTTPException(  # noqa: B904
                status.HTTP_503_SERVICE_UNAVAILABLE,
                "face_recognition non installe (pip install -e .[ml])",
            )
        except Exception as e:
            logger.warning("face_detect_failed photo=%s err=%r", photo.id, e)
            errors += 1
            continue

        for f in faces:
            db.add(
                PhotoFace(
                    photo_id=photo.id,
                    bbox=f["bbox"],
                    encoding=f["encoding"],
                    model_name=f"dlib_{payload.detection_model}",
                )
            )
            faces_found += 1
        photos_processed += 1

    await db.commit()

    return FaceDetectResponse(
        photos_processed=photos_processed,
        faces_found=faces_found,
        errors=errors,
        duration_seconds=round(time.monotonic() - start, 2),
    )


@router.post("/cluster-faces", response_model=ClusterRunResponse)
async def cluster_faces(
    db: Annotated[AsyncSession, Depends(get_db)],
    eps: float = 0.5,
    min_samples: int = 2,
) -> ClusterRunResponse:
    """Lance DBSCAN sur TOUS les PhotoFace, assigne cluster_id.

    Reset complet : on supprime les clusters existants (sauf names manuels)
    et on recree. Les noms manuels sont reassignes au cluster equivalent
    via le centroid le plus proche.

    Note : pour <5000 visages c'est tres rapide (<5s). Au-dela, prevoir HNSW + faiss.
    """
    try:
        from src.services.face_detector import (
            FaceRecognitionNotInstalledError,
            cluster_encodings,
        )
    except ImportError:
        raise HTTPException(  # noqa: B904
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "sklearn indispo (pip install -e .[ml])",
        )

    start = time.monotonic()

    # Charge tous les faces
    stmt = select(PhotoFace.id, PhotoFace.encoding, PhotoFace.cluster_id)
    rows = (await db.execute(stmt)).all()
    if not rows:
        return ClusterRunResponse(
            total_faces=0,
            clusters_found=0,
            noise_faces=0,
            duration_seconds=0,
        )

    face_ids = [r[0] for r in rows]
    encodings = [r[1] for r in rows]

    # Sauvegarde les noms existants AVANT reset
    named_clusters = (
        (await db.execute(select(FaceCluster).where(FaceCluster.name.is_not(None)))).scalars().all()
    )
    named_centroids: dict[str, list[float]] = {}
    if named_clusters:
        import numpy as np

        for nc in named_clusters:
            members = (
                (await db.execute(select(PhotoFace.encoding).where(PhotoFace.cluster_id == nc.id)))
                .scalars()
                .all()
            )
            if members:
                named_centroids[nc.name] = np.array(members).mean(axis=0).tolist()

    try:
        labels = cluster_encodings(encodings, eps=eps, min_samples=min_samples)
    except FaceRecognitionNotInstalledError:
        raise HTTPException(  # noqa: B904
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "sklearn DBSCAN indispo (pip install -e .[ml])",
        )

    # Reset : delete tous les clusters et reassigne
    await db.execute(FaceCluster.__table__.delete())

    # Map label -> new cluster_id
    import numpy as np

    label_to_cluster: dict[int, UUID] = {}
    cluster_counts: dict[int, int] = {}
    cluster_centroids: dict[int, list[float]] = {}

    encs_array = np.array(encodings, dtype=np.float32)
    for label in set(labels):
        if label == -1:  # bruit
            continue
        members_idx = [i for i, l in enumerate(labels) if l == label]  # noqa: E741
        cluster_centroids[label] = encs_array[members_idx].mean(axis=0).tolist()
        cluster_counts[label] = len(members_idx)

    # Reassigne les noms aux clusters par centroide le plus proche
    name_assignments: dict[int, str] = {}
    for name, old_centroid in named_centroids.items():
        if not cluster_centroids:
            break
        old_arr = np.array(old_centroid, dtype=np.float32)
        # Distance euclidienne
        best_label = min(
            cluster_centroids.keys(),
            key=lambda l: np.linalg.norm(  # noqa: E741
                np.array(cluster_centroids[l]) - old_arr
            ),
        )
        # Threshold raisonnable
        if np.linalg.norm(np.array(cluster_centroids[best_label]) - old_arr) < 0.6:
            name_assignments[best_label] = name

    # Cree les nouveaux FaceClusters
    for label, count in cluster_counts.items():
        members_idx = [i for i, l in enumerate(labels) if l == label]  # noqa: E741
        cluster = FaceCluster(
            name=name_assignments.get(label),
            photo_count=count,
            sample_face_id=face_ids[members_idx[0]],
        )
        db.add(cluster)
        await db.flush()  # pour avoir l'id
        label_to_cluster[label] = cluster.id

    # Update PhotoFace.cluster_id
    noise_count = 0
    for face_id, label in zip(face_ids, labels, strict=False):
        if label == -1:
            new_id = None
            noise_count += 1
        else:
            new_id = label_to_cluster.get(label)
        await db.execute(
            PhotoFace.__table__.update().where(PhotoFace.id == face_id).values(cluster_id=new_id)
        )

    await db.commit()

    return ClusterRunResponse(
        total_faces=len(rows),
        clusters_found=len(label_to_cluster),
        noise_faces=noise_count,
        duration_seconds=round(time.monotonic() - start, 2),
    )


@router.get("/face-clusters", response_model=list[FaceClusterOut])
async def list_face_clusters(
    db: Annotated[AsyncSession, Depends(get_db)],
    only_named: bool = False,
    limit: int = 100,
) -> list[FaceClusterOut]:
    """Liste les clusters tries par photo_count desc."""
    stmt = select(FaceCluster).order_by(desc(FaceCluster.photo_count)).limit(limit)
    if only_named:
        stmt = stmt.where(FaceCluster.name.is_not(None))
    rows = (await db.execute(stmt)).scalars().all()
    return [FaceClusterOut.model_validate(r) for r in rows]


@router.patch("/face-clusters/{cluster_id}", response_model=FaceClusterOut)
async def rename_cluster(
    cluster_id: UUID,
    payload: FaceClusterRename,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> FaceClusterOut:
    """Renomme un cluster (Marc le tag avec un prenom)."""
    cluster = await db.get(FaceCluster, cluster_id)
    if cluster is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Cluster not found")
    cluster.name = payload.name
    await db.commit()
    await db.refresh(cluster)
    return FaceClusterOut.model_validate(cluster)


@router.get("/by-face/{cluster_id}", response_model=list[dict[str, Any]])
async def photos_by_face(
    cluster_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    limit: int = 200,
) -> list[dict[str, Any]]:
    """Retourne les photos contenant un visage de ce cluster."""
    stmt = (
        select(Photo.id, Photo.media_id, Photo.filename, Photo.taken_at)
        .join(PhotoFace, PhotoFace.photo_id == Photo.id)
        .where(PhotoFace.cluster_id == cluster_id)
        .order_by(desc(Photo.taken_at))
        .limit(limit)
    )
    rows = (await db.execute(stmt)).all()
    return [
        {
            "photo_id": str(r[0]),
            "media_id": r[1],
            "filename": r[2],
            "taken_at": r[3].isoformat() if r[3] else None,
        }
        for r in rows
    ]


@router.get("/ml-status")
async def ml_status(db: Annotated[AsyncSession, Depends(get_db)]) -> dict[str, Any]:
    """Status des features ML : installes? combien d'embeddings/faces?"""
    clip_ok = True
    face_ok = True
    try:
        import open_clip  # noqa: F401
        import torch  # noqa: F401
    except ImportError:
        clip_ok = False
    try:
        import face_recognition  # noqa: F401
        from sklearn.cluster import DBSCAN  # noqa: F401
    except ImportError:
        face_ok = False

    total_photos = (await db.execute(select(func.count()).select_from(Photo))).scalar_one() or 0
    total_embeddings = (
        await db.execute(select(func.count()).select_from(PhotoEmbedding))
    ).scalar_one() or 0
    total_faces = (await db.execute(select(func.count()).select_from(PhotoFace))).scalar_one() or 0
    total_clusters = (
        await db.execute(select(func.count()).select_from(FaceCluster))
    ).scalar_one() or 0

    return {
        "clip_installed": clip_ok,
        "face_recognition_installed": face_ok,
        "total_photos": int(total_photos),
        "total_embeddings": int(total_embeddings),
        "embed_remaining": int(total_photos) - int(total_embeddings),
        "total_faces": int(total_faces),
        "total_clusters": int(total_clusters),
    }
