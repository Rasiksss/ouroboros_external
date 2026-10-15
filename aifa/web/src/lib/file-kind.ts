import {
  File,
  FileArchive,
  FileCode,
  FileImage,
  FileSpreadsheet,
  FileText,
  FileType,
  Presentation,
  type LucideIcon,
} from "lucide-react";

/**
 * Вид файла по расширению: иконка + человеческое имя формата.
 *
 * Раньше все вложения рисовались одним значком, и по чипу нельзя было понять,
 * что именно приложено. Значки подобраны так, чтобы три ходовых формата
 * различались ФОРМОЙ, а не только подписью: таблица — сетка, текстовый
 * документ — буква, отчёт PDF — строки текста.
 */
export type FileKind = {
  /** Как называть формат в подсказке: «PDF-документ», «Таблица Excel». */
  label: string;
  Icon: LucideIcon;
};

const BY_EXT: Record<string, FileKind> = {
  pdf: { label: "PDF-документ", Icon: FileText },

  doc: { label: "Документ Word", Icon: FileType },
  docx: { label: "Документ Word", Icon: FileType },
  rtf: { label: "Документ RTF", Icon: FileType },
  odt: { label: "Документ ODT", Icon: FileType },

  xls: { label: "Таблица Excel", Icon: FileSpreadsheet },
  xlsx: { label: "Таблица Excel", Icon: FileSpreadsheet },
  xlsm: { label: "Таблица Excel", Icon: FileSpreadsheet },
  ods: { label: "Таблица ODS", Icon: FileSpreadsheet },
  csv: { label: "Таблица CSV", Icon: FileSpreadsheet },
  tsv: { label: "Таблица TSV", Icon: FileSpreadsheet },

  ppt: { label: "Презентация", Icon: Presentation },
  pptx: { label: "Презентация", Icon: Presentation },
  odp: { label: "Презентация", Icon: Presentation },

  txt: { label: "Текстовый файл", Icon: FileText },
  md: { label: "Текст Markdown", Icon: FileText },

  png: { label: "Изображение", Icon: FileImage },
  jpg: { label: "Изображение", Icon: FileImage },
  jpeg: { label: "Изображение", Icon: FileImage },
  gif: { label: "Изображение", Icon: FileImage },
  webp: { label: "Изображение", Icon: FileImage },
  svg: { label: "Изображение", Icon: FileImage },

  zip: { label: "Архив", Icon: FileArchive },
  rar: { label: "Архив", Icon: FileArchive },
  "7z": { label: "Архив", Icon: FileArchive },

  json: { label: "Файл JSON", Icon: FileCode },
  xml: { label: "Файл XML", Icon: FileCode },
  html: { label: "Файл HTML", Icon: FileCode },
};

const UNKNOWN: FileKind = { label: "Файл", Icon: File };

/** Расширение файла заглавными («PDF», «XLSX») или пусто, если его нет. */
export function fileExtOf(filename: string): string {
  const m = /\.([A-Za-z0-9]{1,5})$/.exec(filename.trim());
  return m ? m[1].toUpperCase() : "";
}

export function fileKindOf(filename: string): FileKind {
  const ext = fileExtOf(filename).toLowerCase();
  return BY_EXT[ext] ?? UNKNOWN;
}
