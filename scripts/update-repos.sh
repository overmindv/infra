#!/usr/bin/env bash
# Безопасно обновляет локальные main всех репозиториев workspace и CodeGraph.

set -u

ROOT="$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)"
DRY=0
BRANCH=""

while [ $# -gt 0 ]; do
  case "$1" in
    --root)
      ROOT="${2:-}"
      [ -n "$ROOT" ] || { echo "--root: требуется путь" >&2; exit 2; }
      shift 2
      ;;
    --branch)
      BRANCH="${2:-}"
      [ -n "$BRANCH" ] || { echo "--branch: требуется имя ветки" >&2; exit 2; }
      shift 2
      ;;
    --dry-run)
      DRY=1
      shift
      ;;
    --force)
      echo "--force больше не поддерживается: локальные изменения не удаляются" >&2
      exit 2
      ;;
    *)
      echo "неизвестная опция: $1" >&2
      exit 2
      ;;
  esac
done

ROOT="$(CDPATH= cd -- "$ROOT" && pwd)"

default_branch() {
  local repo="$1" branch
  branch=$(git -C "$repo" symbolic-ref --short refs/remotes/origin/HEAD 2>/dev/null | sed 's#^origin/##')
  if [ -n "$branch" ]; then
    printf '%s\n' "$branch"
    return
  fi
  branch=$(git -C "$repo" for-each-ref --format='%(refname:short)' 'refs/remotes/origin/*' 2>/dev/null \
    | sed -n 's#^origin/\(main\|master\)$#\1#p' | head -1)
  printf '%s\n' "${branch:-main}"
}

repo_name() {
  local repo="$1"
  if [ "$repo" = "$ROOT" ]; then
    printf '%s\n' "(корень проекта)"
  else
    printf '%s\n' "${repo#"$ROOT"/}"
  fi
}

repos=()
manifest="$ROOT/infra/agent-services.json"
scope_failed=0
if [ -f "$manifest" ]; then
  while IFS= read -r name; do
    repo="$ROOT/$name"
    if git -C "$repo" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
      repos+=("$repo")
    else
      echo "! $name: репозиторий из agent-services.json отсутствует" >&2
      scope_failed=1
    fi
  done < <(python3 -c 'import json, sys; print("\n".join(json.load(open(sys.argv[1]))["services"]))' "$manifest")
else
  if git -C "$ROOT" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    repos+=("$ROOT")
  fi
  for repo in "$ROOT"/*/; do
    [ -e "$repo/.git" ] && repos+=("${repo%/}")
  done
fi

if [ "$scope_failed" -ne 0 ]; then
  echo "Обновление отменено: workspace неполный" >&2
  exit 1
fi

if [ "${#repos[@]}" -eq 0 ]; then
  echo "Не найдено ни одного git-репозитория в $ROOT"
  exit 0
fi

echo "Корень: $ROOT"
echo "Репозиториев найдено: ${#repos[@]}"

if [ "$DRY" -eq 1 ]; then
  for repo in "${repos[@]}"; do
    branch=$(default_branch "$repo")
    [ -n "$BRANCH" ] && branch="$BRANCH"
    echo "[dry] $(repo_name "$repo"): fetch origin/$branch; switch $branch; merge --ff-only"
  done
  if [ -d "$ROOT/.codegraph" ]; then
    echo "[dry] codegraph sync $ROOT"
  else
    echo "[dry] codegraph init $ROOT"
  fi
  exit 0
fi

if ! command -v codegraph >/dev/null 2>&1; then
  echo "codegraph не найден в PATH; обновление не начато" >&2
  exit 1
fi

# Общий preflight выполняется до fetch, checkout и merge в любом репозитории.
preflight_failed=0
for repo in "${repos[@]}"; do
  name=$(repo_name "$repo")
  changes=$(git -C "$repo" status --porcelain --untracked-files=normal)
  if [ -n "$changes" ]; then
    echo "! $name: есть незакоммиченные изменения"
    preflight_failed=1
  fi
  if [ -f "$(git -C "$repo" rev-parse --git-path MERGE_HEAD)" ] \
    || [ -d "$(git -C "$repo" rev-parse --git-path rebase-merge)" ] \
    || [ -d "$(git -C "$repo" rev-parse --git-path rebase-apply)" ]; then
    echo "! $name: незавершённая merge/rebase операция"
    preflight_failed=1
  fi
done
if [ "$preflight_failed" -ne 0 ]; then
  echo "Обновление отменено на preflight; локальные файлы не изменены"
  exit 1
fi

# Сначала fetch для всех репозиториев: сетевой сбой не должен приводить к частичному merge.
declare -a branches=()
for index in "${!repos[@]}"; do
  repo="${repos[$index]}"
  branch=$(default_branch "$repo")
  [ -n "$BRANCH" ] && branch="$BRANCH"
  branches[$index]="$branch"
  echo "== $(repo_name "$repo"): fetch origin/$branch"
  if ! git -C "$repo" fetch --quiet --prune origin "$branch"; then
    echo "! $(repo_name "$repo"): fetch не удался; checkout и merge не выполнялись" >&2
    exit 1
  fi
done

# После fetch заранее обнаруживаем diverged main до первого checkout/merge.
for index in "${!repos[@]}"; do
  repo="${repos[$index]}"
  branch="${branches[$index]}"
  if git -C "$repo" show-ref --verify --quiet "refs/heads/$branch"; then
    if ! git -C "$repo" merge-base --is-ancestor "$branch" "origin/$branch" \
      && ! git -C "$repo" merge-base --is-ancestor "origin/$branch" "$branch"; then
      echo "! $(repo_name "$repo"): $branch и origin/$branch разошлись; fast-forward невозможен"
      echo "  Разрешите расхождение локально и повторите make update"
      exit 1
    fi
  fi
done

for index in "${!repos[@]}"; do
  repo="${repos[$index]}"
  branch="${branches[$index]}"
  name=$(repo_name "$repo")
  echo "== $name: обновление $branch"
  if git -C "$repo" show-ref --verify --quiet "refs/heads/$branch"; then
    if ! git -C "$repo" switch --quiet "$branch"; then
      echo "! $name: не удалось переключиться на $branch" >&2
      exit 1
    fi
  elif ! git -C "$repo" switch --quiet -c "$branch" --track "origin/$branch"; then
    echo "! $name: не удалось создать tracking-ветку $branch" >&2
    exit 1
  fi
  if ! git -C "$repo" merge --quiet --ff-only "origin/$branch"; then
    echo "! $name: fast-forward не выполнен; разрешите конфликт локально" >&2
    exit 1
  fi
done

if [ -d "$ROOT/.codegraph" ]; then
  echo "== CodeGraph: sync"
  codegraph sync "$ROOT"
else
  echo "== CodeGraph: init"
  codegraph init "$ROOT"
fi

echo "Workspace обновлён успешно"
