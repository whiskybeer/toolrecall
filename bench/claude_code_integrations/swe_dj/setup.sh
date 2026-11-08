#!/usr/bin/env bash
# setup.sh [DIR] — build the SWE-bench Lite django/db/migrations task set used by ab_swe.py.
# Needs: the SWE-bench Lite dataset in the Hugging Face cache (or network), git, uv, pyarrow.
set -euo pipefail
DIR=${1:-/home/hermes/bench-data/swe-dj}; HERE="$(cd "$(dirname "$0")" && pwd)"
mkdir -p "$DIR/inst" "$DIR/bin"; cp "$HERE/grade_dj.py" "$HERE/setup_tests.py" "$DIR/"
sed "s#/home/hermes/bench-data/swe-dj#$DIR#" "$HERE/dj-test" > "$DIR/bin/dj-test"; chmod +x "$DIR/bin/dj-test"
python3 - "$DIR" <<'PY'
import glob, json, os, sys
import pyarrow.parquet as pq
d = sys.argv[1]
src = glob.glob(os.path.expanduser("~/.cache/huggingface/hub/datasets--princeton-nlp--SWE-bench_Lite/snapshots/*/data/test-*.parquet"))
if not src:
    sys.exit("SWE-bench Lite parquet not in the HF cache: huggingface-cli download princeton-nlp/SWE-bench_Lite --repo-type dataset")
ids = {"django__django-15738", "django__django-15996", "django__django-16595", "django__django-16820", "django__django-17087"}
for r in pq.read_table(src[0]).to_pylist():
    if r["instance_id"] in ids:
        o = f"{d}/inst/{r['instance_id']}"; os.makedirs(o, exist_ok=True)
        for k in ("base_commit", "patch", "test_patch", "problem_statement"):
            open(f"{o}/{k}", "w").write(r[k])
        json.dump({"f2p": json.loads(r["FAIL_TO_PASS"]), "p2p": json.loads(r["PASS_TO_PASS"])}, open(f"{o}/tests.json", "w"))
PY
[ -d "$DIR/django" ] || git clone -q --filter=blob:none https://github.com/django/django.git "$DIR/django"
[ -x "$DIR/env/bin/python" ] || { uv venv -q -p 3.11 "$DIR/env" && VIRTUAL_ENV="$DIR/env" uv pip install -q "asgiref>=3.6" "sqlparse>=0.3.1"; }
python3 "$DIR/setup_tests.py"
# Gold baseline + validation: unpatched must fail f2p, gold patch must pass both.
for f in "$DIR"/inst/*; do
  ID=$(basename "$f"); G="$DIR/gold-$ID"
  git -C "$DIR/django" worktree add -q -f --detach "$G" "$(cat "$f/base_commit")"
  u=$("$DIR/env/bin/python" "$DIR/grade_dj.py" "$ID" "$G")
  (cd "$G" && patch -p1 -s -i "$f/patch")
  "$DIR/env/bin/python" "$DIR/grade_dj.py" "$ID" "$G" --record-gold > /dev/null
  echo "$ID unpatched[$u] gold[$("$DIR/env/bin/python" "$DIR/grade_dj.py" "$ID" "$G")]"
  git -C "$DIR/django" worktree remove --force "$G"
done
