"""Оркестраторы прогона: собирают все стадии и отдают события наружу.

Здесь `_extract_stream_full_context` — полноконтекстное извлечение, которое
режет реестр на шарды по 20 показателей и запускает их параллельно, а рядом
держит две отдельные стадии: разрезы и состав холдинга.

⚠️ ЭТОТ МОДУЛЬ ПОДЛЕЖИТ ДАЛЬНЕЙШЕМУ РАЗБОРУ. Один только
`_extract_stream_full_context` — 944 строки. У Алексея он разложен на
streams_full_context / streams_unrestricted / streams_sync; сделать так же,
отдельным шагом и с тем же условием — поведение не меняется.
"""
from __future__ import annotations

from .streams_metric_iter import (  # noqa: E402
    _metric_block_iter,  # noqa: F401
)
from .streams_unrestricted import (  # noqa: E402
    _extract_stream_unrestricted,  # noqa: F401
)
from .streams_sync import (  # noqa: E402
    _extract_async,  # noqa: F401
    extract_all_sync,  # noqa: F401
)
from .streams_dev import (  # noqa: E402
    extract_stream_finengine_dev,  # noqa: F401
)
from .streams_full_context import (  # noqa: E402
    _merge_aliases_from_base,  # noqa: F401
    _extract_stream_full_context,  # noqa: F401
)








