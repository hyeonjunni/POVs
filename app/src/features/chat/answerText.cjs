function answerText(value) {
  return String(value ?? "")
    .replace(/^\s*```[^\n]*\n?/gm, "")
    .replace(/^\s{0,3}#{1,6}\s+/gm, "")
    .replace(/\*\*([^*\n]+)\*\*/g, "$1")
    .replace(/__([^_\n]+)__/g, "$1")
    .replace(/\*([^*\n]+)\*/g, "$1")
    .replace(/`([^`\n]+)`/g, "$1")
    .replace(/\[([^\]\n]+)\]\(([^)\n]+)\)/g, "$1 ($2)")
    .replace(/^\s*[-*+]\s+/gm, "• ")
    .trim();
}

module.exports = { answerText };
