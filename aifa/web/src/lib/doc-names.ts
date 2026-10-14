/**
 * Имена только что загруженных документов — крошечный общий реестр.
 *
 * Зачем: ход чата рисует в пузыре сообщения приложенные документы, а имя берёт
 * из списка документов рабочего экрана. Список приезжает с сервера асинхронно,
 * и в момент отправки (сразу после дозагрузки) документа в нём ещё нет — чип
 * пропадал, хотя файл ушёл. Загрузка кладёт имя сюда СИНХРОННО, поэтому гонки
 * не остаётся.
 */
const names = new Map<string, string>();

export function rememberDocName(docId: string, filename: string): void {
  if (docId && filename) names.set(docId, filename);
}

export function docNameOf(docId: string): string | undefined {
  return names.get(docId);
}
