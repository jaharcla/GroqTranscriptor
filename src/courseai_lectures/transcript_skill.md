<callout icon="🧾" color="blue_bg">
	**Purpose:** evaluate an imported AI-generated transcript, preserve uncertainty, route it to the correct Course and Module, and integrate only verified course content into the existing academic workspace.
</callout>
## Required inputs
- Raw imported transcript, preferably with timestamps.
- Any available recording date, class time, instructor, filename, or source page.
- Existing Course, Module, Lecture Inbox, and official course-source pages when available.
## Safety and policy gate
1. Find or infer the candidate Course before transforming the transcript.
2. Treat course material as available for personal-study processing, while checking whether the transcript is being used for a specific graded task with its own restriction.
3. If a specific graded submission prohibits AI use, do not use the transcript workflow to produce or transform that submission; personal-study transcript cleanup and organization can still proceed.
4. Preserve the raw transcript as the source. Never overwrite or delete it while creating a reviewed version.
## Workflow
1. Locate the canonical Lecture Inbox item or lecture page. Reuse it instead of creating a duplicate.
2. Identify candidate Courses using explicit course codes, filenames, class dates, instructor names, schedule context, topic vocabulary, and matches to existing Course or Module sources.
3. Assign a Course and Module only when the evidence is sufficiently specific. If multiple candidates remain plausible, ask the user to choose; do not guess.
4. Validate transcript wording against the raw context and available course sources:
	- Correct only clear, high-confidence speech-to-text errors.
	- Use surrounding grammar, repeated terms, course glossaries, formulas, units, proper nouns, and official sources as evidence.
	- Preserve instructor-specific notation and terminology.
	- Do not invent missing words, silently resolve ambiguous claims, or convert an uncertain statement into a fact.
5. Classify transcript material:
	- **Course content:** explanations, definitions, mechanisms, worked examples, demonstrations, exam cues, and relevant instructor commentary.
	- **Course context:** references to readings, assignments, modules, dates, logistics, or student questions that help interpret the lecture.
	- **Non-course speech:** greetings, filler, recording artifacts, unrelated conversation, room logistics, and side chatter.
	- Keep borderline material when removing it could change meaning; label it for review rather than discarding it.
6. Produce a clean reviewed transcript. Remove obvious filler and unrelated speech only when the meaning remains intact. Keep timestamps or nearby anchors for substantive corrections and unresolved passages.
7. Integrate the result into existing pages:
	- Add the reviewed transcript and a compact review record to the canonical lecture page or Lecture Inbox item.
	- Link the confirmed Course and Module.
	- Add course-relevant verified information to the existing Module or Course page only when it is directly supported by the transcript and appropriate to that page.
	- Do not create duplicate Course, Module, or Source records.
	- Preserve source links and distinguish transcript-only claims from claims confirmed by official sources.
8. Mark processing as ready only after the transcript has been reviewed enough to serve as a study source. Leave unresolved items visible.
## Confidence rules
- **High confidence:** correct directly and record the original wording, replacement, reason, and evidence.
- **Medium confidence:** retain the most likely wording only if the meaning is unchanged; otherwise flag it for user review.
- **Low confidence:** preserve the raw wording or mark the passage as unintelligible; never fabricate a completion.
- Treat a course-source disagreement as a conflict to show, not as permission to silently rewrite the transcript.
## Output
Return the following in this order:
1. **Routing:** Course, Module, lecture date, and assignment confidence; list competing candidates if unresolved.
2. **Reviewed transcript:** the clean transcript, organized into meaningful paragraphs or sections while preserving important timestamps.
3. **Review flags:** only unresolved words, meaning-changing corrections, source conflicts, and passages needing audio confirmation. For each, include the transcript wording, proposed wording if any, confidence, and reason.
4. **Excluded speech:** briefly list what was removed or separated as non-course speech; do not silently hide substantive content.
5. **Integration:** identify the lecture, Course, and Module pages updated or linked, and state which claims were transcript-only versus source-confirmed.
6. **Next action:** ask only the smallest necessary clarification, such as choosing between two candidate Courses or confirming a low-confidence technical term.
## Handoff guidance
Use the reviewed transcript as the source for downstream study artifacts. Invoke Course Material Ingest & Study Guide, Formula Definition and Diagram Extractor, Smart Flashcard Generator, or other study skills only when the user requests those outputs or when the integration workflow explicitly calls for them. Keep all generated study material linked to the reviewed transcript and the supporting Course sources.
