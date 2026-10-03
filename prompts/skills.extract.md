---
{
  "system": "You extract skill terms from text. You return terms that appear in or are directly named by the text, never terms you assume follow from them.",
  "schema": {
    "type": "object",
    "required": ["skills"],
    "properties": {
      "skills": {
        "type": "array",
        "items": {
          "type": "object",
          "required": ["term"],
          "properties": {
            "term": {"type": "string"},
            "evidence": {"type": "string"}
          }
        }
      }
    }
  }
}
---
List the skills named in the text below.

Return the term as the text writes it — the taxonomy resolves spellings, so do
not normalise, expand or translate. Do not infer: someone who lists Django has
not thereby claimed PostgreSQL. Quote the phrase that shows each one.

Text:

{text}
