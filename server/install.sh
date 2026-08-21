#!/usr/bin/env bash
set -euo pipefail

if [[ "${EUID}" -ne 0 ]]; then
  echo "Запустите скрипт через sudo." >&2
  exit 1
fi

project_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
install_root="/opt/codex-stats-bot"
data_root="/var/lib/codex-stats-bot"
env_file="/etc/codex-stats-bot.env"

if ! id codexstats >/dev/null 2>&1; then
  useradd --system --home-dir "${data_root}" --shell /usr/sbin/nologin codexstats
fi

install -d -o root -g root -m 0755 "${install_root}"
install -d -o codexstats -g codexstats -m 0750 "${data_root}"
rm -rf "${install_root}/server"
cp -a "${project_root}/server" "${install_root}/server"
find "${install_root}" -type f -name '*.py' -exec chmod 0644 {} +

if [[ ! -f "${env_file}" ]]; then
  install -o root -g codexstats -m 0640 "${project_root}/server/.env.example" "${env_file}"
  echo "Создан ${env_file}. Замените CODEX_STATS_API_KEY и настройте Telegram."
fi

install -o root -g root -m 0644 "${project_root}/server/codex-stats-bot.service" /etc/systemd/system/codex-stats-bot.service
systemctl daemon-reload

echo "Установка завершена. После редактирования ${env_file} выполните:"
echo "  sudo systemctl enable --now codex-stats-bot"
echo "  systemctl status codex-stats-bot"

