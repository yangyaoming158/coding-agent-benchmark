/**
 * 日志搜索的文本工具。
 *
 * 为什么不写在组件文件里：带 JSX 的文件编出来是 React 组件，想在 node 里
 * 直接断言它就得多背一套运行环境。而搜索恰恰是最该钉住的逻辑 ——
 * 大小写、空关键词、一行里出现多次、匹配落在行首行尾，每一处写错的表现
 * 都是"看着正常但搜不到"，靠肉眼在几千行日志里发现不了。
 */

/**
 * 把一行按关键词切成交替的片段：`[普通, 匹配, 普通, 匹配, ……, 普通]`。
 * **偶数下标是普通文本，奇数下标是匹配到的原文**，长度恒为奇数。
 *
 * 大小写不敏感（用 `toLowerCase` 找位置，切分仍按原文，高亮后大小写不变）。
 * 匹配在行首/行尾时会产生空片段，这是故意的：片段下标的奇偶性要稳定，
 * 否则调用方得判断"这一段到底是匹配还是普通"。
 *
 * `maxParts` 是防御：一行里如果有成千上万处匹配（比如搜单个字母），
 * 不设上限会造出几万个 DOM 节点把页面卡死。
 */
export function splitByQuery(line: string, query: string, maxParts = 50): string[] {
  if (query === "") return [line];

  const lowerLine = line.toLowerCase();
  const lowerQuery = query.toLowerCase();

  // 极罕见的 Unicode 情形：大小写转换改变了字符串长度，索引会错位。
  // 日志里不会出现，但错位的后果是切出乱码，不如整体不切。
  if (lowerLine.length !== line.length || lowerQuery.length !== query.length) {
    return [line];
  }

  const parts: string[] = [];
  let cursor = 0;
  for (let i = 0; i < maxParts; i++) {
    const found = lowerLine.indexOf(lowerQuery, cursor);
    if (found === -1) break;
    parts.push(line.slice(cursor, found));
    parts.push(line.slice(found, found + query.length));
    cursor = found + query.length;
  }
  parts.push(line.slice(cursor));
  return parts;
}

/**
 * 哪些行包含关键词（返回 0 开头的行号）。到 `limit` 就停 ——
 * 搜索的用途是定位，不是把整个日志重新渲染一遍。
 */
export function findMatchingLines(
  lines: readonly string[],
  query: string,
  limit: number,
): number[] {
  if (query === "") return [];
  const lowerQuery = query.toLowerCase();
  const hits: number[] = [];
  for (let i = 0; i < lines.length && hits.length < limit; i++) {
    if (lines[i].toLowerCase().includes(lowerQuery)) hits.push(i);
  }
  return hits;
}
