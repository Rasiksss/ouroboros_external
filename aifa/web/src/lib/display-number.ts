/** Единое форматирование чисел на экране; исходное значение evidence не меняется. */
export function formatDisplayNumber(
  value: number,
  maximumFractionDigits = 3,
  minimumFractionDigits = 0,
): string {
  // Ненулевое значение не должно выглядеть нулём: «-0,0004» вместо «-0».
  if (value !== 0 && Math.abs(value) < 0.5 * 10 ** -maximumFractionDigits) {
    return value.toLocaleString("ru-RU", { maximumSignificantDigits: 3 });
  }
  return (value + 0).toLocaleString("ru-RU", { maximumFractionDigits, minimumFractionDigits });
}
