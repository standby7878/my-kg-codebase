# Frozen prompts

Prompts are immutable benchmark inputs. The manifest binds each prompt ID to a file, task family, and version; freeze the manifest before schedule generation. Every prompt begins with the no-modification preamble and asks for evidence-aware answers. Do not add follow-up questions or repair prompts after a trial has started.

The frozen families cover discovery, structural navigation, constructors, uncertainty zones, impact, and complexity. Keep repository, commit, prompt ID, and condition out of the prompt text where possible so scoring remains blind.

The template below is a starting shape for reviewed prompt files. It is not a substitute for truth review.

```text
You are analyzing the pinned repository in the supplied checkout.
Do not modify, create, delete, format, or execute files. Do not suggest that you made changes.
State uncertainty explicitly. Separate observed evidence from inference, and name symbols/files used as evidence.

Task family: <discovery|navigation|constructors|uncertainty|impact|complexity>
Question: <reviewed question>
Response format: <reviewed concise format>
```

## Manifest

`manifest.json` is the frozen list; its `sha256` values are filled only after prompt content is reviewed and committed. A manifest entry must not point outside this directory.
