# Workstation контура ouroboros (ADR-0007): база `workstation-svc:local` + слой пакетов комнаты.
# Без кода finengine и без моста моделей. Базы с этим тегом на dev нет: её восстанавливает
# restore_base.py из слоёв workstation-svc-aifa:local (P34, README.md).
#   docker build -f aifa/deploy/dev/workstation/Dockerfile -t aifa-sym-workstation:dev aifa/deploy/dev/workstation
FROM workstation-svc:local
USER root

# Landlock ws-sandbox запрещает читать /etc; импорт openpyxl вызывает mimetypes.init(), который
# открывает /etc/mime.types и падает с PermissionError. Без файла mimetypes его пропускает.
RUN rm -f /etc/mime.types

# Пакеты комнаты — в системный site-packages: именно его читает python сессии ws-sandbox
# (/opt/venv и /usr/local ей закрыты). Версии numpy, pandas, openpyxl — как у комнаты старого dev
# (pip freeze 15.10.2026), чтобы вычисления атомов совпадали с опорной записью (P30); python-pptx
# и python-docx в старой комнате нет, версии — на дату сборки.
RUN /usr/bin/python3.12 -m pip install --no-cache-dir \
        --target=/usr/lib64/python3.12/site-packages \
        numpy==2.5.0 pandas==3.0.3 openpyxl==3.1.5 python-pptx==1.0.2 python-docx==1.1.2

# База оставляет это на рантайм: ws-sandbox и busybox должны быть исполняемыми.
RUN chmod +x /usr/local/bin/ws-sandbox /usr/local/libexec/workstation/busybox

USER aef
