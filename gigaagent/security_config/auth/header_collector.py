from pathlib import Path

import yaml

from infra.security_context.declaration import HeaderCollectorDeclaration

header_collector_declaration = HeaderCollectorDeclaration.model_validate(
    yaml.safe_load(Path(__file__).with_name("header_collector.yaml").read_text(encoding="utf-8"))
)
