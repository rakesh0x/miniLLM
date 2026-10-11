Q1. which ai tools/models did you use, and for what?

i mainly used claude opus through my own harness, with AGENTS.md files to give it context about different parts of the repo.

i used it for research, discussing design choices, and implementation. it was especially useful when i asked it to challenge my ideas instead of simply following my instructions. i still reviewed the code myself and kept anything involving credits and billing under my control.

Q2. one place the ai was wrong, and how you caught it.

i built a response cache with 82 passing tests, but it wasn't working in production. the issue was that my next.js proxy sent a no-cache header, which caused the backend to skip the cache.

all the tests passed because they called the backend directly and never covered the full request flow. i caught it by testing the deployed setup end to end and added regression tests for both cached requests and intentional cache bypasses.

Q3. one place you overrode the ai's suggestion, and why.

i initially wanted semantic caching with embeddings and pgvector, so differently worded questions could reuse the same answer. before implementing it, i asked claude to argue against the idea.

embedding calls would add latency to every request, and similar questions could still require different answers. i decided to stick with exact-match caching because a missed cache hit is better than returning an incorrect answer. i'd reconsider it later if real usage data justifies the extra complexity.

Q4. how did you stay in control of code you didn't write yourself?

i used AGENTS.md files to define project rules, a decision log to keep track of earlier choices, and approval requirements for sensitive changes.

anything involving credentials, billing logic, configuration, or destructive database operations needed my approval. this helped me focus on important decisions, like how credits are reserved and what happens when a request fails, instead of reviewing every line of generated code equally.

Q5. something you had to learn from scratch this weekend.

i spent time understanding database concurrency and why postgres was a better fit than sqlite for my credit system.

i also learned that async def doesn't automatically make blocking operations non-blocking. i'd moved one database call to a threadpool but left three others running directly on the event loop. my concurrency tests showed roughly a 6× performance penalty, which helped me identify what needed fixing.