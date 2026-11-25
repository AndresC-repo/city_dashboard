"""One-off patch for src/city/process.py on the cluster: remove isolated noise points before the models.

Before: only points classified as noise (ASPRS 7, 18) were dropped. Surveys that never classified
their noise (e.g. Helena 2012) kept spikes hundreds of metres up, and the model made "trees" of them.
After: the labelling tool's rule (TreeLabellingTool backend/services/noise.py, used on every
training patch when it is cut) is applied too, so city tiles are cleaned like the training data:

  a point is noise if it lies more than max(10 m, half the tile's normal height range) above the
  99.5th or below the 0.5th percentile of the tile's heights, AND has fewer than 5 such points
  within 5 m. Tall trees and roofs above the normal range are dense clusters and stay.

stats/<tile>.json gets noise_classified and noise_outliers (noise_points stays the total removed).
Switch off with NOISE_OUTLIER_FILTER = False at the top of process.py.

Run once on the cluster, in the repo folder:
    python patch_process_noise.py src/city/process.py
Edits in place (backup process.py.bak); refuses if the code looks different. Already processed
tiles are not changed; new runs (and REDO=1) use it.
"""
import shutil
import sys

path = sys.argv[1] if len(sys.argv) > 1 else "src/city/process.py"
s = open(path).read()
if "def noise_outliers(" in s:
    sys.exit("already patched")

anchor_const = "NOISE_CLASSES = (7, 18)\n"
old_keep = '''    keep = ~np.isin(cls, NOISE_CLASSES)
    n_raw = len(xyz)
    stats = dict(tile_id=tile["tile_id"], points_raw=int(n_raw), noise_points=int((~keep).sum()))
'''
for part in (anchor_const, old_keep):
    if part not in s:
        sys.exit(f"process.py differs from the expected code, nothing changed. Missing:\n{part}")

s = s.replace(anchor_const, anchor_const + '''
# Isolated far outliers = noise, same rule and values as the labelling tool (TreeLabellingTool
# backend/services/noise.py), so city tiles are cleaned like the training patches. Keep both in step.
NOISE_OUTLIER_FILTER = True
NOISE_MIN_MARGIN_M = 10.0
NOISE_NEIGHBOUR_RADIUS_M = 5.0
NOISE_MIN_NEIGHBOURS = 5


def noise_outliers(xyz: np.ndarray, keep: np.ndarray) -> np.ndarray:
    """Points (among `keep`) far outside the tile's normal height range AND isolated. Metres."""
    out = np.zeros(len(xyz), dtype=bool)
    if not NOISE_OUTLIER_FILTER or keep.sum() < 100:
        return out
    z = xyz[:, 2]
    z_lo, z_hi = np.percentile(z[keep], [0.5, 99.5])
    margin = max(NOISE_MIN_MARGIN_M, 0.5 * (z_hi - z_lo))
    idx = np.flatnonzero(keep & ((z > z_hi + margin) | (z < z_lo - margin)))
    if len(idx):
        from scipy.spatial import cKDTree
        pts = xyz[idx]
        n_near = cKDTree(pts).query_ball_point(pts, r=NOISE_NEIGHBOUR_RADIUS_M, return_length=True) - 1
        out[idx[n_near < NOISE_MIN_NEIGHBOURS]] = True
    return out
''', 1)

s = s.replace(old_keep, '''    keep = ~np.isin(cls, NOISE_CLASSES)
    n_raw = len(xyz)
    n_noise_class = int((~keep).sum())
    outliers = noise_outliers(xyz, keep)         # isolated spikes far above / below the tile
    keep &= ~outliers
    stats = dict(tile_id=tile["tile_id"], points_raw=int(n_raw), noise_points=int((~keep).sum()),
                 noise_classified=n_noise_class, noise_outliers=int(outliers.sum()))
''', 1)

shutil.copy(path, path + ".bak")
open(path, "w").write(s)
print(f"patched {path} (backup {path}.bak)")
