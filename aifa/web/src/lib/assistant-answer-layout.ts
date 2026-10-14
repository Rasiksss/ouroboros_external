import type { TextMessage } from "./projects";

export type AssistantAnswerLayout = {
  contentPlacement: "before-results" | "after-results" | null;
  showTypedResults: boolean;
};

/** Чистое правило видимого ответа: карточки дополняют текст, analysisCard по-прежнему заменяет его. */
export function assistantAnswerLayout(
  message: TextMessage,
  streaming: boolean,
): AssistantAnswerLayout {
  const showTypedResults = (message.typedResults?.length ?? 0) > 0;
  if (!message.content || message.analysisCard || streaming) {
    return { contentPlacement: null, showTypedResults };
  }
  const dynamicAnswer = Boolean(message.dynamicTrace);
  return {
    contentPlacement: showTypedResults && !dynamicAnswer ? "after-results" : "before-results",
    showTypedResults,
  };
}
