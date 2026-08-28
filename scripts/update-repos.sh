#!/usr/bin/env bash
# update-repos.sh — одной командой подтягивает последние изменения для всех
# git-репозиториев, лежащих внутри корня проекта (overmindv), из дефолтной
# ветки их удалённого репозитория (обычно main, fallback master).
#
# Для каждого репозитория:
#   git fetch --prune origin
#   переход на локальную дефолтную ветку (создаёт её из origin, если нет)
#   git pull --ff-only origin <default>
#
# Безопасность по умолчанию: репозитории с незакоммиченными изменениями
# пропускаются (не теряют данные). Чтобы форс-сбросить их до origin, см. --force.
#
# Использование:
#   ./update-repos.sh [--root DIR] [--force] [--dry-run]
#
#   --root DIR   корневая директория со сканированием (по умолчанию — родитель
#                каталога infra, т.е. сам overmindv); сканируются сам корень
#                и его прямые подпапки, содержащие .git
#   --branch BR  форсировать ветку BR во всех репозиториях (например --branch main)
#   --force      в репозиториях с правками выполнить reset --hard до origin
#                (локальные изменения будут отброшены!)
#   --dry-run    только показать, что будет сделано, ничего не менять

set -u

ROOT="$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)"
FORCE=0
DRY=0
BRANCH=""

while [ $# -gt 0 ]; do
  case "$1" in
    --root)  ROOT="${2:-}"; [ -n "$ROOT" ] || { echo "--root: требуется путь" >&2; exit 2; }; shift 2 ;;
    --branch) BRANCH="${2:-}"; [ -n "$BRANCH" ] || { echo "--branch: требуется имя ветки" >&2; exit 2; }; shift 2 ;;
    --force) FORCE=1; shift ;;
    --dry-run) DRY=1; shift ;;
    *) echo "неизвестная опция: $1" >&2; exit 2 ;;
  esac
done

ROOT="$(CDPATH= cd -- "$ROOT" && pwd)"

# default_branch DIR — имя дефолтной ветки удалённого репозитория.
# Приоритет: origin/HEAD -> origin/main/master -> main.
default_branch() {
  local d="$1" b
  b=$(git -C "$d" symbolic-ref --short refs/remotes/origin/HEAD 2>/dev/null | sed 's#^origin/##')
  if [ -n "$b" ]; then printf '%s\n' "$b"; return; fi
  b=$(git -C "$d" for-each-ref --format='%(refname:short)' 'refs/remotes/origin/*' 2>/dev/null \
      | sed -n 's#^origin/\(main\|master\)$#\1#p' | head -1)
  printf '%s\n' "${b:-main}"
}

# Собираем список репозиториев: сам корень (если это git) + прямые подпапки с .git.
repos=""
if git -C "$ROOT" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  repos="$ROOT"
fi
for d in "$ROOT"/*/; do
  [ -d "$d/.git" ] && repos="$repos
$d"
done

[ -n "$repos" ] || { echo "Не найдено ни одного git-репозитория в $ROOT" >&2; exit 0; }

echo "Корень: $ROOT"
echo "Репозиториев найдено: $(printf '%s\n' "$repos" | sed '/^$/d' | wc -l | tr -d ' ')"
echo

# NOTE: $repos содержит пути без переводов строк, поэтому безопасно читать построчно.
printf '%s\n' "$repos" | sed '/^$/d' | while IFS= read -r r; do
  name="${r#"$ROOT"/}"
  [ "$r" = "$ROOT" ] && name="(корень проекта)"
  br="$(default_branch "$r")"
  [ -n "$BRANCH" ] && br="$BRANCH"
  echo "== $name  ->  origin/$br"

  if [ "$DRY" = 1 ]; then
    echo "   [dry] git fetch --prune origin; checkout $br; pull --ff-only origin $br"
    continue
  fi

  if ! git -C "$r" fetch --prune origin; then
    echo "   ! fetch не удался — пропущено"
    continue
  fi

  if ! git -C "$r" diff --quiet || ! git -C "$r" diff --cached --quiet; then
    if [ "$FORCE" = 1 ]; then
      git -C "$r" checkout -f "$br" 2>/dev/null
      if ! git -C "$r" reset --hard "origin/$br"; then
        echo "   ! reset --hard не удался"; continue
      fi
      echo "   ! форс-обновлено до origin/$br (локальные правки отброшены)"
    else
      echo "   ! есть незакоммиченные изменения — пропущено (--force чтобы сбросить)"
    fi
    continue
  fi

  if ! git -C "$r" checkout "$br" 2>/dev/null; then
    # ветки нет локально — создаём из origin
    if ! git -C "$r" switch -c "$br" --track "origin/$br"; then
      echo "   ! не удалось переключиться на $br"; continue
    fi
    echo "   (создана локальная ветка $br из origin/$br)"
  fi

  if ! git -C "$r" pull --ff-only origin "$br"; then
    echo "   ! pull не fast-forward — ветка локально разошлась с удалённой, требуется ручное вмешательство"
  fi
done
