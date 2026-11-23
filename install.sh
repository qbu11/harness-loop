#!/usr/bin/env bash
set -euo pipefail

# Zero-config installer for harness-loop across Antigravity, Claude Code, Codex, and future harnesses.
# Supports both remote `curl -fsSL ... | bash` and local `bash ./install.sh`.

REPO_URL="${HARNESS_LOOP_REPO_URL:-${AGY_LOOP_REPO_URL:-https://github.com/qbu11/harness-loop.git}}"
BRANCH="${HARNESS_LOOP_BRANCH:-feat/quota-aware-routing}"
SKILL_NAME="harness-loop"
AGENTS_SKILLS_DIR="$HOME/.agents/skills"
CANONICAL_DIR="$AGENTS_SKILLS_DIR/$SKILL_NAME"
INIT_AGENTS_DIR=""

usage() {
  cat <<'EOF'
Usage: install.sh [--branch <name>] [--init-agents <repo_dir>] [--help]

Installs harness-loop into ~/.agents/skills/harness-loop and mounts symlinks
for Claude Code (~/.claude/skills), Codex (~/.codex/skills), and Antigravity
(~/.gemini/config/skills), then runs `loopctl.py doctor`.

Options:
  --branch <name>           Git branch to install when cloning (default: feat/quota-aware-routing)
  --init-agents <repo_dir>  Also append the 3-line role template to <repo_dir>/AGENTS.md
  -h, --help                Show this help message
EOF
}

while [ $# -gt 0 ]; do
  case "$1" in
    --branch)
      if [ $# -lt 2 ]; then
        echo "ERROR: --branch requires a branch name argument" >&2
        exit 1
      fi
      BRANCH="$2"
      shift 2
      ;;
    --init-agents)
      if [ $# -lt 2 ]; then
        echo "ERROR: --init-agents requires a repository path argument" >&2
        exit 1
      fi
      INIT_AGENTS_DIR="$2"
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "ERROR: Unknown argument: $1" >&2
      usage >&2
      exit 1
      ;;
  esac
done

PY_BIN="$(command -v python3 || true)"
if [ -z "$PY_BIN" ]; then
  echo "ERROR: python3 (>= 3.10) is required." >&2
  exit 1
fi

realpath_py() {
  "$PY_BIN" -B -c 'import os,sys; print(os.path.realpath(sys.argv[1]))' "$1"
}

SRC_ROOT=""
if [ -n "${BASH_SOURCE[0]:-}" ] && [ -f "${BASH_SOURCE[0]}" ]; then
  CAND_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
  if [ -f "$CAND_DIR/SKILL.md" ] && [ -f "$CAND_DIR/scripts/loopctl.py" ]; then
    SRC_ROOT="$CAND_DIR"
  fi
fi

mkdir -p "$AGENTS_SKILLS_DIR"
TS="$(date +%Y%m%d%H%M%S)"

if [ -z "$SRC_ROOT" ]; then
  if ! command -v git >/dev/null 2>&1; then
    echo "ERROR: git is required when installing from remote." >&2
    exit 1
  fi
  if [ -d "$CANONICAL_DIR/.git" ]; then
    echo "Updating existing git checkout at $CANONICAL_DIR (branch: $BRANCH)..."
    if [ -z "$(git -C "$CANONICAL_DIR" status --porcelain)" ]; then
      git -C "$CANONICAL_DIR" fetch origin "$BRANCH"
      git -C "$CANONICAL_DIR" checkout "$BRANCH"
      git -C "$CANONICAL_DIR" pull --ff-only origin "$BRANCH"
    else
      echo "WARN: $CANONICAL_DIR has local changes; skipping git pull."
    fi
  else
    if [ -e "$CANONICAL_DIR" ] || [ -L "$CANONICAL_DIR" ]; then
      BACKUP="${CANONICAL_DIR}.bak.${TS}"
      echo "Backing up existing $CANONICAL_DIR -> $BACKUP"
      mv "$CANONICAL_DIR" "$BACKUP"
    fi
    echo "Cloning $REPO_URL (branch: $BRANCH) -> $CANONICAL_DIR..."
    git clone -b "$BRANCH" "$REPO_URL" "$CANONICAL_DIR"
  fi
else
  REAL_SRC="$(realpath_py "$SRC_ROOT")"
  AGENTS_REAL="$(realpath_py "$AGENTS_SKILLS_DIR")"
  TARGET_CANON="$AGENTS_REAL/$SKILL_NAME"
  if [ "$REAL_SRC" = "$TARGET_CANON" ]; then
    echo "Already at canonical skill location: $CANONICAL_DIR"
  else
    if [ -e "$CANONICAL_DIR" ] || [ -L "$CANONICAL_DIR" ]; then
      BACKUP="${CANONICAL_DIR}.bak.${TS}"
      echo "Backing up existing $CANONICAL_DIR -> $BACKUP"
      mv "$CANONICAL_DIR" "$BACKUP"
    fi
    mkdir -p "$CANONICAL_DIR"
    tar --exclude='.agent-loop' --exclude='__pycache__' -C "$SRC_ROOT" -cf - . | tar -C "$CANONICAL_DIR" -xf -
    echo "Installed local copy -> $CANONICAL_DIR"
  fi
fi

chmod +x \
  "$CANONICAL_DIR/install.sh" \
  "$CANONICAL_DIR/scripts/loopctl.py" \
  "$CANONICAL_DIR/scripts/quota_router.py" \
  "$CANONICAL_DIR/scripts/audit_commits.py" \
  "$CANONICAL_DIR/scripts/test_loopctl.py" 2>/dev/null || true

CANON_REAL="$(realpath_py "$CANONICAL_DIR")"
AGENTS_REAL="$(realpath_py "$AGENTS_SKILLS_DIR")"

link_skill_dir() {
  local label="$1"
  local parent_dir="$2"
  mkdir -p "$parent_dir"
  local parent_real
  parent_real="$(realpath_py "$parent_dir")"
  if [ "$parent_real" = "$AGENTS_REAL" ]; then
    echo "  OK  $label: ALREADY_LINKED_VIA_PARENT ($parent_dir -> $parent_real)"
    return 0
  fi

  local target="$parent_dir/$SKILL_NAME"
  if [ -L "$target" ] || [ -e "$target" ]; then
    local target_real
    target_real="$(realpath_py "$target")"
    if [ "$target_real" = "$CANON_REAL" ]; then
      echo "  OK  $label: $target -> $CANON_REAL"
      return 0
    fi
    local bak="${target}.bak.${TS}"
    echo "  WARN $label: backing up existing $target -> $bak"
    mv "$target" "$bak"
  fi

  ln -s "$CANONICAL_DIR" "$target"
  echo "  OK  $label: linked $target -> $CANONICAL_DIR"
}

echo ""
echo "Mounting symlinks across harnesses..."
link_skill_dir "Claude Code" "$HOME/.claude/skills"
link_skill_dir "Codex" "$HOME/.codex/skills"
link_skill_dir "Antigravity" "$HOME/.gemini/config/skills"

if [ -n "$INIT_AGENTS_DIR" ]; then
  echo ""
  echo "Appending AGENTS.md 3-line harness role snippet -> $INIT_AGENTS_DIR"
  "$PY_BIN" -B "$CANONICAL_DIR/scripts/loopctl.py" snippet --append "$INIT_AGENTS_DIR"
fi

echo ""
"$PY_BIN" -B "$CANONICAL_DIR/scripts/loopctl.py" doctor
