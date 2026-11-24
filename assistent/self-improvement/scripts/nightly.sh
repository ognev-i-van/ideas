#!/bin/bash
# Pre-run для ночного самообучения: архив скилов (откат), затем выгрузка сессий за сутки.
# Лежит в <профиль>/scripts/; вывод session_digest.py уходит в промпт задачи.
set -u
P=$(cd "$(dirname "$0")/.." && pwd)
NAME=$(basename "$P")
B=${SELFLEARN_BACKUP_DIR:-$P/backups/selflearn}
KEEP=${SELFLEARN_KEEP:-14}
mkdir -p "$B"
tar czhf "$B/$NAME-skills-$(date +%Y%m%d).tgz" -C "$P" skills 2>/dev/null  # -h: с содержимым симлинков
ls -1t "$B/$NAME"-skills-*.tgz 2>/dev/null | tail -n +$((KEEP + 1)) | xargs -r rm --
exec python3 -I "$P/scripts/session_digest.py" --home "$P" --days "${SELFLEARN_DAYS:-1}"
