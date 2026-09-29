"""Stage 5 - Anomaly detection (spec section 11.3).

Real, trained, persisted models - not rules:

* **IsolationForest** (primary).  Unsupervised: it isolates observations that are
  easy to separate, which in this dataset is exactly "the transaction that does
  not look like its neighbours" - huge amounts, extreme fee ratios, unusual
  fan-in/fan-out.
* **LocalOutlierFactor** (secondary, optional).  Density-based, catches the
  *local* weirdness that a global tree ensemble can miss - e.g. a normally-sized
  transaction that is bizarre compared with its own neighbourhood.

Both are trained on the transaction feature matrix, rank-normalised to [0, 1] and
blended; the top ``ANOMALY_FLAG_QUANTILE`` of the blended score is flagged.  The
fitted models (plus the exact feature order) are saved with joblib so a demo run
never needs to retrain, and the same class is reused for wallet-level anomalies.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Optional, Sequence

import joblib
import numpy as np
import pandas as pd
from sklearn.ensemble import IsolationForest
from sklearn.neighbors import LocalOutlierFactor
from sklearn.preprocessing import StandardScaler

from src import config

logger = logging.getLogger(__name__)


def _rank_normalise(values: np.ndarray) -> np.ndarray:
    """Percentile rank in [0, 1] where 1.0 = most anomalous."""
    if len(values) == 0:
        return values
    order = values.argsort().argsort().astype(float)
    denominator = max(len(values) - 1, 1)
    return order / denominator


@dataclass
class AnomalyResult:
    scores: pd.DataFrame = field(default_factory=pd.DataFrame)   # id, iso_score, lof_score, anomaly_score, anomaly_flag
    feature_importance: Dict[str, float] = field(default_factory=dict)
    model_path: Optional[str] = None
    n_flagged: int = 0

    def score_map(self) -> Dict[str, float]:
        if self.scores.empty:
            return {}
        return dict(zip(self.scores.iloc[:, 0].astype(str), self.scores["anomaly_score"].astype(float)))

    def flag_map(self) -> Dict[str, bool]:
        if self.scores.empty:
            return {}
        return dict(zip(self.scores.iloc[:, 0].astype(str), self.scores["anomaly_flag"].astype(bool)))

    def summary(self) -> Dict[str, Any]:
        if self.scores.empty:
            return {"scored": 0, "flagged": 0}
        return {
            "scored": int(len(self.scores)),
            "flagged": int(self.scores["anomaly_flag"].sum()),
            "mean_score": round(float(self.scores["anomaly_score"].mean()), 4),
            "model": "IsolationForest" + (" + LOF" if "lof_score" in self.scores.columns else ""),
        }


class AnomalyDetector:
    """Isolation Forest (+ optional LOF) wrapper with joblib persistence."""

    def __init__(
        self,
        feature_columns: Sequence[str],
        id_column: str = "txid",
        contamination: float = config.ISOLATION_FOREST_CONTAMINATION,
        use_lof: bool = config.USE_LOF_MODEL,
        seed: int = config.RANDOM_SEED,
        name: str = "transaction",
    ) -> None:
        self.feature_columns = list(feature_columns)
        self.id_column = id_column
        self.contamination = contamination
        self.use_lof = use_lof
        self.seed = seed
        self.name = name
        self.model: Optional[IsolationForest] = None
        self.lof: Optional[LocalOutlierFactor] = None
        self.scaler: Optional[StandardScaler] = None
        self.fitted = False

    # ------------------------------------------------------------------ #
    def _matrix(self, frame: pd.DataFrame) -> pd.DataFrame:
        missing = [c for c in self.feature_columns if c not in frame.columns]
        if missing:
            raise ValueError(f"Missing feature columns for anomaly model: {missing}")
        matrix = frame[self.feature_columns].apply(pd.to_numeric, errors="coerce")
        return matrix.fillna(0.0).astype(float)

    def fit(self, frame: pd.DataFrame) -> "AnomalyDetector":
        matrix = self._matrix(frame)
        self.scaler = StandardScaler().fit(matrix.values)
        scaled = self.scaler.transform(matrix.values)

        self.model = IsolationForest(
            n_estimators=config.ISOLATION_FOREST_ESTIMATORS,
            contamination=self.contamination,
            max_samples=config.ISOLATION_FOREST_MAX_SAMPLES,
            random_state=self.seed,
            n_jobs=-1,
        ).fit(scaled)

        if self.use_lof and len(matrix) > (config.LOF_NEIGHBORS + 1):
            try:
                self.lof = LocalOutlierFactor(
                    n_neighbors=min(config.LOF_NEIGHBORS, max(2, len(matrix) - 1)),
                    novelty=True,
                    contamination=self.contamination,
                ).fit(scaled)
            except Exception as exc:  # pragma: no cover - defensive
                logger.warning("LOF training failed (%s) - continuing with IsolationForest only", exc)
                self.lof = None
        else:
            self.lof = None

        self.fitted = True
        logger.info(
            "Trained %s anomaly model on %d rows x %d features (%s)",
            self.name, len(matrix), len(self.feature_columns),
            "IsolationForest + LOF" if self.lof is not None else "IsolationForest",
        )
        return self

    # ------------------------------------------------------------------ #
    def score(self, frame: pd.DataFrame) -> AnomalyResult:
        if not self.fitted or self.model is None or self.scaler is None:
            raise RuntimeError("AnomalyDetector must be fitted before scoring")
        matrix = self._matrix(frame)
        scaled = self.scaler.transform(matrix.values)

        # higher = more anomalous
        iso_raw = -self.model.score_samples(scaled)
        iso_score = _rank_normalise(iso_raw)

        if self.lof is not None:
            lof_raw = -self.lof.decision_function(scaled)
            lof_score = _rank_normalise(lof_raw)
            weight = config.LOF_WEIGHT
            blended = (1.0 - weight) * iso_score + weight * lof_score
        else:
            lof_score = np.zeros_like(iso_score)
            blended = iso_score

        blended = _rank_normalise(blended)
        threshold = float(np.quantile(blended, config.ANOMALY_FLAG_QUANTILE))

        result = pd.DataFrame(
            {
                self.id_column: frame[self.id_column].astype(str).values,
                "iso_score": np.round(iso_score, 6),
                "lof_score": np.round(lof_score, 6),
                "anomaly_score": np.round(blended, 6),
                "iso_raw": np.round(iso_raw, 6),
                "anomaly_flag": blended >= threshold,
            }
        )
        importance = self.feature_importance(matrix)
        return AnomalyResult(
            scores=result,
            feature_importance=importance,
            n_flagged=int(result["anomaly_flag"].sum()),
        )

    # ------------------------------------------------------------------ #
    def feature_importance(self, matrix: pd.DataFrame, repeats: int = 3) -> Dict[str, float]:
        """Global importance.

        Uses ``IsolationForest.feature_importances_`` when scikit-learn provides it,
        otherwise a permutation importance computed directly against the model's
        anomaly score (model-agnostic and dependency-free).
        """
        if self.model is None or self.scaler is None:
            return {}
        if hasattr(self.model, "feature_importances_"):
            values = np.asarray(self.model.feature_importances_, dtype=float)
            if values.size == len(self.feature_columns) and values.sum() > 0:
                values = values / values.sum()
                return dict(zip(self.feature_columns, np.round(values, 6).tolist()))

        # permutation importance
        frame = matrix.copy()
        scaled = self.scaler.transform(frame.values)
        base = -self.model.score_samples(scaled)
        importance = np.zeros(frame.shape[1])
        rng = np.random.default_rng(self.seed)
        for column in range(frame.shape[1]):
            deltas = []
            for _ in range(repeats):
                shuffled = frame.copy()
                shuffled.iloc[:, column] = rng.permutation(shuffled.iloc[:, column].values)
                perturbed = -self.model.score_samples(self.scaler.transform(shuffled.values))
                deltas.append(float(np.mean(perturbed - base)))
            importance[column] = max(float(np.mean(deltas)), 0.0)
        total = importance.sum()
        if total > 0:
            importance = importance / total
        return dict(zip(self.feature_columns, np.round(importance, 6).tolist()))

    # ------------------------------------------------------------------ #
    def save(self, path: Optional[Path | str] = None) -> Path:
        path = Path(path or (config.MODEL_DIR / f"{self.name}_anomaly_model.joblib"))
        path.parent.mkdir(parents=True, exist_ok=True)
        joblib.dump(
            {
                "model": self.model,
                "lof": self.lof,
                "scaler": self.scaler,
                "feature_columns": self.feature_columns,
                "id_column": self.id_column,
                "contamination": self.contamination,
                "use_lof": self.use_lof,
                "name": self.name,
                "seed": self.seed,
            },
            path,
        )
        return path

    @classmethod
    def load(cls, path: Path | str) -> "AnomalyDetector":
        payload = joblib.load(path)
        detector = cls(
            feature_columns=payload["feature_columns"],
            id_column=payload.get("id_column", "txid"),
            contamination=payload.get("contamination", config.ISOLATION_FOREST_CONTAMINATION),
            use_lof=payload.get("use_lof", False),
            seed=payload.get("seed", config.RANDOM_SEED),
            name=payload.get("name", "transaction"),
        )
        detector.model = payload["model"]
        detector.lof = payload.get("lof")
        detector.scaler = payload["scaler"]
        detector.fitted = True
        return detector


def detect_anomalies(
    features: pd.DataFrame,
    feature_columns: Sequence[str],
    id_column: str = "txid",
    name: str = "transaction",
    save_path: Optional[Path | str] = None,
) -> AnomalyResult:
    """Fit + score in one call (used by the pipeline and scripts/train_models.py)."""
    detector = AnomalyDetector(feature_columns=feature_columns, id_column=id_column, name=name)
    detector.fit(features)
    result = detector.score(features)
    if save_path is not None:
        result.model_path = str(detector.save(save_path))
    else:
        result.model_path = str(detector.save())
    return result
