# Coding Style Guide

Some best practices to consider while you code and doc.


## Text Guidance

- Prefer U.S. English spelling (e.g. "color" not "colour", "normalize" not "normalise").
- Prefer active voice.  This includes documentation, comments, and user messages.
- Use the Oxford Comma.
- Avoid emoji, non-ASCII characters (fancy quotes or dashes), or fancy characters used as part of art.  Only use extended characters for testing localization compatibility or text encoding or security tests or edge cases in tests, and, in those cases, prefer unicode escape sequences over the UTF-8 character.
    - As a caveat, use puny code for URL hostnames (`xn--` prefixes), to make the name clear and avoid glyph confusion.
- Do not line-break MarkDown paragraphs.  Keep the text as a single line, to which editors and viewers can apply their preferred line wrapping.
- When writing documentation (markdown or HTML), use SVG files for non-trivial drawings.


## Constants and Names

- Avoid magic numbers and strings by extracting recurring or meaningful values into descriptive constants (const) or enums. Keep self-explanatory, one-off values inline to avoid clutter. If a value comes from a spec (e.g. HTTP 200 OK), use a constant regardless.
- Where reasonable, include the unit name in the variable or constant - `TIMEOUT_SECONDS` over `TIMEOUT`.  Terms like "size" can cause confusion.  If the value permits setting the unit (e.g. `30 mb` or `7 days`), then use a unit-less name.
- Keep function names short. Less than 30 characters.
    - Do not start a test function name with the article (such as `func a_value_must_be_empty()` or `fn an_animal_moves()` or `def test_the_humidor_is_full()`).  Such extra characters do not aid in reader comprehension.
    - Tests can have longer names if the function-under-test has a long name, but it also should indicate the need to shorten the function-under-test's name.
- Use enums instead of booleans for function parameters.


## Code Flow

- Reduce code indentation. Avoid Arrow Anti-Pattern. Leverage early return and continue.
- Let the reader of the code breathe. Add empty lines between logical blocks of code.
- Add a small, to the point, comment to explain *what* the block does and *why*. Use examples when possible. Propose ASCII drawings to explain complete systems.
- Program to levels of abstraction. Lower-level mechanics (e.g., raw hardware I/O, sector parsing, direct socket streams) must be encapsulated in a dedicated driver/abstraction layer. Expose clean, high-level APIs to the rest of the application so calling code works with domain concepts, not raw implementation details.
- Generally attempt to maintain a DRY (don't repeat yourself) approach.  However, this applies only to shared functionality which also share a same purpose - there exist functionality with a similar shape, but an overzealous attempt at unifying the functionality can cause later bugs when the requirements around the functionality change in one part, but the invoking code does not share the same requirements.


## Rust

- Always use `{}`, even on a one-line "if" statement.
- Annotate references where elision over-constrains.  Unless absolutely required, structures holding references can allow them to come from multiple lifecycle contexts.  This means prefer `struct Name<'a, 'b>{ left: &'a Value, right: &'b Value }` to `struct Name { left: &Value, right: &Value }`.  This can also happen in some call signatures.  However, terseness wins over this.


## Python

- Use type hints everywhere.
- Anywhere you write-off ruff or bandit notices, add a comment why you did it.
- Python data-driven unit tests use 'subTest' to distinguish which ones fail, and to keep running when any one fails.

## Shell Scripts

- Prefer `sh` over `bash`.
- Be aware of MacOS versus GNU tool differences.
- Prefer `${var_name}` over bare `$var_name` - it makes the variable reference stand out.
