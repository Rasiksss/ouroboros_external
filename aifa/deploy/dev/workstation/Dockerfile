# Workstation контура ouroboros (ADR-0007): база `workstation-svc:local` + слой пакетов комнаты.
# Без кода finengine и без моста моделей; база есть только на dev (P4), локально не собирается.
#   docker build -f aifa/deploy/dev/workstation/Dockerfile -t aifa-sym-workstation:dev aifa/deploy/dev/workstation
FROM workstation-svc:local
USER root

# Landlock ws-sandbox запрещает читать /etc; импорт openpyxl вызывает mimetypes.init(), который
# открывает /etc/mime.types и падает с PermissionError. Без файла mimetypes его пропускает.
RUN rm -f /etc/mime.types

# Пакеты комнаты — в системный site-packages: именно его читает python сессии ws-sandbox
# (/opt/venv и /usr/local ей закрыты).
RUN /usr/bin/python3.12 -m pip install --no-cache-dir \
        --target=/usr/lib64/python3.12/site-packages \
        numpy pandas openpyxl python-pptx python-docx

# База оставляет это на рантайм: ws-sandbox и busybox должны быть исполняемыми.
RUN chmod +x /usr/local/bin/ws-sandbox /usr/local/libexec/workstation/busybox

USER aef
