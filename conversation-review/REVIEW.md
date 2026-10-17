# Conversational drive assistant: implementation review

Implemented on featureconversational-drive-ai. Main was not changed. No commit or push was made.

## Validation

- 135 automated tests pass in the live project; git diff --check is clean.
- Existing nonfatal Tk ThemeChanged teardown warning remains in the test output.
- The tree no longer waits for synchronous database indexing. Background indexing and cancellation were checked, including a synthetic Tk smoke test.
- A real raw-volume MFT scan was not performed in this validation.
- C: cleanup candidates and manual file-operation controls retain their existing behaviour. The new conversational tools are read-only.

## Real local Qwen trial

Seven questions used synthetic scan records and synthetic approved text. No personal files were read or changed. Model loading was warmed separately.

- Both size wordings returned exactly the same result: 977 KB.
- Useful facts took 4.9-5.3 seconds. Explanations took 13.6-23.7 seconds.
- First displayed explanation text arrived in 8.5-16.3 seconds.
- No fallback occurred; two explanations were marked partial.
- All sentences passed a simple length check. This is not ASD-STE100 vocabulary validation or certification.

## Quality failures that remain

This prototype is not approved as consistently accurate cleanup advice. The following model claims exceed the evidence:

- Config: it invented a "Project app" from a folder name. The evidence establishes a JSON setting, not an application owner.
- References: it said main.py reads config.json "at startup". A source-code reference does not establish execution timing.
- Duplicates: it said content verification "failed". Content comparison was not performed. The enforced closing note correctly says the files are only possible duplicates, but the earlier claim is still wrong.
- Several explanations omit the requested full item path even though it is supplied to the model.

The grounding rules already prohibit these claims. Real testing shows that instructions and structured output alone do not make this 4B model reliable enough. Another quality iteration must address semantic claim checking or model capability, and measure the extra latency. Do not treat passing tool tests as proof of explanation accuracy.

## Files

See RESPONSES.md for unedited model replies and results.json for timings and lookup evidence. The evaluation script can repeat the same run. Tests additionally cover generic metadata filters, dates, counts, follow-up focus, missing paths, bounded text search, read-only access and paraphrase handling.

The 15-second goal remains a target, with a 30-second request ceiling. The measured explanations do not all meet 15 seconds.
