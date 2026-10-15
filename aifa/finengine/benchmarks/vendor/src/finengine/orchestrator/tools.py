"""Tool definitions for the Finengine orchestrator."""

TOOLS = [
    {
        "name": "extract_from_documents",
        "description": (
            "Извлекает факты, цифры и цитаты из загруженных финансовых документов "
            "(МСФО, РСБУ, пресс-релизы, транскрипты, презентации). "
            "Работает с текстом и таблицами. Поддерживает русский и английский. "
            "Используй этот инструмент ВСЕГДА, когда нужно найти данные из документов."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "queries": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Список поисковых запросов для извлечения данных. Например: ['выручка 2024', 'revenue growth Q3']",
                },
                "document_filters": {
                    "type": "object",
                    "description": "Фильтры: {doc_ids: [...], doc_type: 'financial_report'|'transcript'|'press_release'|'presentation'|'equity_research'}",
                    "properties": {
                        "doc_ids": {"type": "array", "items": {"type": "string"}},
                        "doc_type": {"type": "string"},
                    },
                },
                "top_k": {
                    "type": "integer",
                    "description": "Количество возвращаемых фрагментов (по умолчанию 8)",
                    "default": 8,
                },
            },
            "required": ["queries"],
        },
    },
    {
        "name": "cross_document_synthesis",
        "description": (
            "Семантический кросс-документный анализ: находит финансовый показатель в таблицах МСФО "
            "и связывает его с комментариями менеджмента из пресс-релизов и транскриптов. "
            "Используй для вопросов типа 'почему изменился показатель X' или 'что говорил CEO о метрике Y'."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "metric": {
                    "type": "string",
                    "description": "Финансовый показатель для анализа, например: 'стоимость риска', 'EBITDA', 'NIM'",
                },
                "period": {
                    "type": "string",
                    "description": "Период анализа, например: '2024', 'Q3 2024', 'H1 2023'",
                },
                "doc_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Опциональный список doc_id для поиска только в конкретных документах",
                },
            },
            "required": ["metric"],
        },
    },
    {
        "name": "factor_analysis",
        "description": (
            "Факторный анализ (Root Cause Analysis): декомпозирует изменение показателя "
            "на составляющие и находит первопричину. "
            "Используй когда нужно понять 'почему упала прибыль' или 'что повлияло на рост расходов'."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "metric": {
                    "type": "string",
                    "description": "Показатель для декомпозиции, например: 'чистая прибыль', 'операционные расходы'",
                },
                "period_current": {
                    "type": "string",
                    "description": "Текущий период, например: '2024' или 'Q3 2024'",
                },
                "period_prev": {
                    "type": "string",
                    "description": "Предыдущий период для сравнения, например: '2023' или 'Q3 2023'",
                },
                "doc_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                },
            },
            "required": ["metric"],
        },
    },
    {
        "name": "create_visualization",
        "description": (
            "Создаёт интерактивный график Plotly из предоставленных данных. "
            "Типы: bar (столбчатый), line (линейный), waterfall (водопад), pie (пирог), table (таблица). "
            "Используй ТОЛЬКО когда у тебя уже есть конкретные числовые данные для визуализации."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "chart_type": {
                    "type": "string",
                    "enum": ["bar", "line", "waterfall", "pie", "table"],
                    "description": "Тип графика",
                },
                "data": {
                    "type": "object",
                    "description": "Данные для графика. Для bar/pie: {categories: [...], values: [...]}. Для line: {x: [...], y_series: {name: [values]}}. Для waterfall: {measures: [...], x: [...], y: [...]}. Для table: {headers: [...], rows: [[...], ...]}",
                },
                "title": {"type": "string", "description": "Заголовок графика"},
                "x_label": {"type": "string", "description": "Подпись оси X"},
                "y_label": {"type": "string", "description": "Подпись оси Y"},
            },
            "required": ["chart_type", "data", "title"],
        },
    },
    {
        "type": "web_search_20260209",
        "name": "web_search",
    },
    {
        "type": "code_execution_20260120",
        "name": "code_execution",
    },
]
