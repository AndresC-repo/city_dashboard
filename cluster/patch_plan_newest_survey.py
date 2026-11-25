"""One-off patch for src/city/plan.py on the cluster: choose the NEWEST, then DENSEST survey.

Before: plan.py took the survey covering the most tiles; when several cover the whole city
(Helena: 2012 and 2022), the first one in the USGS list won, often the oldest and sparsest.
After: among the surveys that cover at least 95% of what the best one covers, the newest wins
(year from the name: "..._2018", "..._B22" = 2022); on the same year, the densest. --survey still
overrides. The printed list shows the year and the order of preference.

Run once on the cluster, in the repo folder:
    python patch_plan_newest_survey.py src/city/plan.py
It edits the file in place (a copy is kept as plan.py.bak) and refuses if the code looks different.
"""
import re
import shutil
import sys

path = sys.argv[1] if len(sys.argv) > 1 else "src/city/plan.py"
s = open(path).read()
if "def survey_year(" in s:
    sys.exit("already patched")

old_sort = "    coverage.sort(key=lambda c: -c[0])\n"
old_print = '''        print(f"  {name:<45} {n:>6} tiles ({n / len(x0):.0%})   about {dens:g} points per m²")\n'''
for part in (old_sort, old_print):
    if part not in s:
        sys.exit(f"plan.py differs from the expected code, nothing changed. Missing:\n{part}")

helper = '''

def survey_year(name: str) -> int:
    """Acquisition year from a survey name: the first 4-digit year ("TX_Central_B1_2017_LAS_2019"
    -> 2017), else a batch suffix like "_B22" / "_D21" -> 2022 / 2021, else 0 (unknown)."""
    m = re.search(r"(?<!\\d)(19\\d\\d|20\\d\\d)(?!\\d)", name)
    if m:
        return int(m.group(1))
    m = re.search(r"_[A-Z](\\d\\d)$", name)
    return 2000 + int(m.group(1)) if m else 0


def load_resources('''
s = s.replace("\n\ndef load_resources(", helper, 1)

new_sort = '''    # Prefer the newest, then the densest survey among those that cover (nearly) everything the
    # best-covering one does; a survey covering clearly fewer tiles comes after them.
    best = max(c[0] for c in coverage)
    def preference(c):
        full = c[0] >= 0.95 * best
        dens = c[4] if c[4] == c[4] else 0.0          # NaN -> 0
        return (not full, -survey_year(c[1]), -dens, -c[0])
    coverage.sort(key=preference)
'''
s = s.replace(old_sort, new_sort, 1)
s = s.replace(old_print, '''        year = survey_year(name)
        print(f"  {name:<45} {n:>6} tiles ({n / len(x0):.0%})   {year or '????'}   about {dens:g} points per m²")
''', 1)
if not re.search(r"^import re\b", s, re.M):
    s = re.sub(r"^(import json\n)", r"\1import re\n", s, count=1, flags=re.M) if re.search(r"^import json\n", s, re.M) \
        else "import re\n" + s

shutil.copy(path, path + ".bak")
open(path, "w").write(s)
print(f"patched {path} (backup {path}.bak)")
