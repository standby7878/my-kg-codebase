# Trial schedules

`example.json` shows a randomized, balanced schedule pattern. B is baseline, M is CodeKG MCP, and MF is the second approved MCP condition. Each corpus/prompt block contains one trial for every condition; randomize condition order with a recorded seed, and balance order/condition across blocks. Do not use the example as an executable schedule until prompts, truth, and corpus pins are signed off.

Record the run ID, block, condition, corpus and commit, prompt ID/version, schedule seed/order, CLI/model versions, start/end times, status, and log path. Preserve failed or interrupted trials as such; never replace their raw logs.
