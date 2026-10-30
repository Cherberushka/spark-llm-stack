#!/usr/bin/env bash
# =============================================================================
# 15-share-doctor.sh — общая папка "doctor" (Samba) с авто-приёмом в конвейер
# Run as root: sudo bash scripts/15-share-doctor.sh
#
# Структура шары \\192.168.11.251\doctor:
#   _incoming  — сотрудники кладут файлы (каждые 5 мин уезжают в kb-inbox
#                и автоматически создают задачу doc-convert)
#   _results   — готовые .md-артефакты выполненных задач шары (чтение)
#   _archive   — ручные архивы отдела
# Доступ: только группа doctor (Linux-группа + samba valid users).
# ВАЖНО: шара — для регламентных документов/шаблонов/обезличенных материалов;
# карты пациентов с ПДн сюда НЕ класть (см. RAG-GUIDE §3/§9).
# =============================================================================
set -euo pipefail
[[ $EUID -eq 0 ]] || { echo "Run as root: sudo bash $0"; exit 1; }
SHARE=/srv/agents/shares/doctor
INBOX=/srv/agents/kb-inbox

echo "=== [1/5] Группа, каталоги, права ==="
groupadd -f doctor
mkdir -p "$SHARE/_incoming" "$SHARE/_results" "$SHARE/_archive" "$INBOX"
chgrp -R doctor "$SHARE"
chmod 770 "$SHARE" "$SHARE/_incoming" "$SHARE/_archive"
chmod 750 "$SHARE/_results"

echo "=== [2/5] Samba ==="
export DEBIAN_FRONTEND=noninteractive
apt-get install -y samba >/dev/null
if ! grep -q '^\[doctor\]' /etc/samba/smb.conf; then
  cat >> /etc/samba/smb.conf <<'EOS'

[doctor]
   path = /srv/agents/shares/doctor
   writable = yes
   valid users = @doctor
   write list = @doctor
   create mask = 0660
   directory mask = 0770
   force group = doctor
EOS
fi
systemctl enable --now smbd nmbd 2>/dev/null || systemctl restart smbd
for net in 192.168.8.0/24 192.168.9.0/24 192.168.10.0/24 192.168.11.0/24; do
  ufw allow from "$net" to any port 445 proto tcp comment 'SMB doctor share' >/dev/null 2>&1 || true
  ufw allow from "$net" to any port 139 proto tcp comment 'SMB legacy' >/dev/null 2>&1 || true
done

echo "=== [3/5] Ingest-скрипт (_incoming -> kb-inbox + задача doc-convert) ==="
cat > /usr/local/sbin/doctor-share-ingest.sh <<'EOS'
#!/usr/bin/env bash
set -uo pipefail
SH=/srv/agents/shares/doctor; IN=/srv/agents/kb-inbox
OKEY=$(grep -oP '(?<=^ORCH_API_KEY=).*' /etc/orchestrator.env)
shopt -s nullglob
for f in "$SH"/_incoming/* "$SH"/*; do
  [[ -f "$f" ]] || continue
  b=$(basename "$f")
  safe=$(python3 -c 'import sys,re;low={"а":"a","б":"b","в":"v","г":"g","д":"d","е":"e","ё":"e","ж":"zh","з":"z","и":"i","й":"y","к":"k","л":"l","м":"m","н":"n","о":"o","п":"p","р":"r","с":"s","т":"t","у":"u","ф":"f","х":"h","ц":"c","ч":"ch","ш":"sh","щ":"sch","ъ":"","ы":"y","ь":"","э":"e","ю":"yu","я":"ya"};d={**{ord(k):v for k,v in low.items()},**{ord(k.upper()):v.capitalize() for k,v in low.items()}};s=re.sub(r"_+","_",re.sub(r"[^A-Za-z0-9._-]+","_",sys.argv[1].translate(d))).strip("._-");print(s or "doc")' "$b")
  [[ -z "$safe" ]] && safe="upload-$(date +%s).bin"
  [[ -e "$IN/$safe" ]] && safe="$(date +%s)-$safe"
  mv "$f" "$IN/$safe"
  TID=$(curl -s -X POST http://127.0.0.1:8500/api/tasks -H "X-Orch-Key: $OKEY" \
    -H 'Content-Type: application/json' \
    -d "{\"template\":\"doc-convert\",\"payload\":{\"file\":\"$safe\"},\"requester\":\"doctor-share\",\"group\":\"doctor\"}" | jq -r .id)
  echo "$(date +%FT%T) ingested '$b' -> $safe task#$TID"
done
# синхронизация результатов: .md выполненных задач шары -> _results
for TID in $(docker exec spark-litellm-db psql -U orchestrator -d agents_tasks -tAc \
  "select id from tasks where requester='doctor-share' and status='done' and updated_at > now()-interval '1 day'" 2>/dev/null); do
  for a in $(curl -s http://127.0.0.1:8500/api/tasks/$TID | jq -r '.task.artifacts[]?'); do
    [[ "$a" == *.md ]] && cp -n "$a" "$SH/_results/" 2>/dev/null
  done
done
EOS
chmod +x /usr/local/sbin/doctor-share-ingest.sh
mkdir -p /var/log/spark-llm
cat > /etc/cron.d/doctor-share <<'EOC'
*/5 * * * * root /usr/local/sbin/doctor-share-ingest.sh >> /var/log/spark-llm/doctor-ingest.log 2>&1
EOC

echo "=== [4/5] Учётные записи ==="
if ! id -u doctor >/dev/null 2>&1; then
  useradd -M -s /usr/sbin/nologin -G doctor doctor
  echo "✓ unix-учётка doctor создана (группа doctor, без shell/home)"
else
  echo "✓ unix-учётка doctor уже есть: $(id doctor)"
fi
cat <<'EOM'
Общая шара: задайте пароль SMB (один раз):  sudo smbpasswd -a doctor
Подключение на Windows:  net use Z: \\192.168.11.251\doctor /user:doctor
Позже — персональные учётки без изменений конвейера:
  sudo useradd -M -s /usr/sbin/nologin -G doctor ivanova && sudo smbpasswd -a ivanova
Отзыв доступа: sudo smbpasswd -x <имя> && sudo gpasswd -d <имя> doctor
EOM

echo "=== [5/5] Проверка ==="
testparm -s 2>/dev/null | grep -A6 '\[doctor\]' | head -8
sudo -u nobody true 2>/dev/null
echo "Windows-подключение:  net use Z: \\\\192.168.11.251\\doctor /user:ivanova"
echo "Журнал приёма: /var/log/spark-llm/doctor-ingest.log"
