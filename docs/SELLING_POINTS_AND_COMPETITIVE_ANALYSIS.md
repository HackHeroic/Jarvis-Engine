# Jarvis — The AI Preparation Engine

**For:** VC pitch, branding, positioning
**Last updated:** 2026-03-31
**Pitch date:** April 1, 2026

---

## What Is Jarvis? (The 10-Second Answer)

> **Jarvis is an AI Preparation Engine.**
>
> You tell it what's on your mind. It figures out what you need to do, breaks it into small steps, builds a schedule that can't have conflicts, reads your documents and connects them to your tasks, and gets smarter every day by learning how you actually work.
>
> **Other tools schedule. Jarvis prepares.**

---

## Why "AI Preparation Engine" — Not Just Another Scheduler

The productivity tool market has a lot of players. Motion schedules. Reclaim blocks time. Sunsama guides your morning ritual. Notion organizes. They each do **one thing well**.

Jarvis does something different. It doesn't just decide *when* you work — it figures out *what* you should work on, *how* to break it down, *what materials* you need, and *how to adapt* when life gets in the way. That's not scheduling. That's **preparation**.

Think of it like this:

- A **calendar** tells you: "You have a meeting at 2 PM"
- A **scheduler** tells you: "Work on physics at 10 AM"
- A **preparation engine** tells you: "Here are 5 specific things to do for physics, each 25 minutes, in the right order, with the practice problems from your uploaded PDF matched to each task, scheduled around your energy patterns, and adjusted based on what you've been skipping lately"

That last one is Jarvis.

---

## The Problem

- **86%** of college students struggle with time management
- **82%** of professionals have no time management system at all
- The average person is productive for just **2 hours 53 minutes** per day
- **80-95%** of students procrastinate — not because they're lazy, but because deciding what to do is harder than doing it

The real problem isn't laziness. **Planning is exhausting.** People spend so much mental energy figuring out what to do and when to do it that they have nothing left for actually doing it. Every existing tool still makes you the planner — they just give you a prettier notebook.

---

## The 10 Things Jarvis Does

### 1. Brain Dump In. Perfect Day Out.

Just tell Jarvis what's on your mind in plain language:

> *"I have a physics exam April 10, I want to learn system design, I don't work well before 11 AM, and here's my syllabus."*

One message. Jarvis extracts your goals, habits, constraints, and deadlines — then builds your entire day. No forms. No dragging blocks. No manual planning.

**Why this matters:** No other tool takes you from "chaos in my head" to "here's your day, step by step" in a single interaction. Motion needs you to create individual tasks first. Reclaim needs you to set up habits manually. Sunsama walks you through a 15-minute morning ritual. Jarvis just... does it.

---

### 2. Schedules That Can't Break

Jarvis uses the same type of technology that powers airline scheduling and factory operations — **constraint programming**. The result:

- Tasks **never overlap**. Double-booking is literally impossible.
- Sleep, classes, and meetings are always protected.
- If Task B needs Task A done first — A always comes first.
- If your goals don't fit in the available time, Jarvis tells you honestly and helps you prioritize — instead of creating an impossible plan.

**The honest comparison:** Motion also does smart scheduling and likely uses sophisticated algorithms (their founders come from quantitative finance). But here's the difference: Jarvis's scheduler is a **mathematical solver** that guarantees constraint satisfaction or returns "this doesn't fit" — it can't silently produce a broken plan. And more importantly, our scheduler is connected to everything else on this list — behavior, memory, documents. Motion's scheduler works in isolation.

---

### 3. Your Schedule Is a Conversation

Every other tool hands you a schedule and says *"deal with it."*

Jarvis proposes a **draft** and asks what you think:

- **"Looks good"** — locked in
- **"Make this one shorter"** — edited, schedule rebuilt instantly
- **"Too packed in the morning"** — Jarvis remembers this and adjusts future plans
- **"Move DSA to the afternoon"** — just say it in plain English
- **Swap task order** — drag and rearrange, schedule rebuilds

Every rejection teaches the system. Every edit makes the next plan better.

**The honest comparison:** Morgen also has a preview-and-approve model for scheduling. They're the closest competitor on this specific feature. But Morgen's negotiation is "approve or move blocks." Jarvis's is richer: reject with a reason (builds memory), chat to modify ("move everything after lunch"), and the system learns your preferences permanently from the conversation.

---

### 4. The AI Learns How You Actually Work

This is the core differentiator. **Jarvis doesn't just remember what you said — it learns from what you did.**

- Skip morning tasks 5 times? Jarvis notices the pattern and stops scheduling mornings.
- Always shorten tasks to 15 minutes? That becomes the new default.
- Reject packed schedules? More breathing room, automatically.

The system detects these patterns with a confidence threshold (needs 3+ observations at 70%+ consistency before it acts), so it doesn't overreact to one-off changes.

**Here's what makes this different from every competitor:** In Motion or Reclaim, if the AI learns your preferences, it changes *what it suggests*. In Jarvis, learned behavior changes the **mathematical constraints in the scheduler itself**. The system literally rewires how it builds your schedule. Your preferences don't just influence the AI's words — they change the math.

**The honest comparison:** Motion claims to "learn your patterns in about 2 weeks." Reclaim learns preferred times for habits. Both are real forms of learning. But they adjust *where* things go on your calendar. Jarvis adjusts *what gets scheduled, how much, and with what constraints* — because behavior feeds into the constraint solver, not just the scheduling heuristic.

---

### 5. Zero Guilt. Always.

When your goals don't fit in the available time, Jarvis doesn't show a red "OVERDUE" badge. Instead:

> *"There's more on your plate than fits today. Want to reprioritize, extend the deadline, or break things into smaller pieces?"*

This isn't just a nice tone. It's a design principle baked into every layer of the product. Research shows guilt-based productivity tools actually make people **less** productive — shame triggers avoidance, which is the opposite of what you need.

Jarvis is built on **mastery orientation**: focus on getting better, not on beating yourself up for falling behind. Every "failure" is a signal to recalibrate, not a reason to feel bad.

**The honest comparison:** Sunsama has a calm, intentional vibe that feels anti-pressure. Saner.AI is designed specifically for people with ADHD and avoids overwhelm. Both care about mental health. But neither has built anti-guilt as a **systemic principle** — where the scheduler's failure mode is "let's adjust" instead of "you failed." In Jarvis, when the solver says "this is impossible," the response is a helpful recalibration conversation, not an error message.

---

### 6. Your Documents Become Part of Your Plan

Upload a practice problems PDF, and Jarvis:

1. Reads and understands the document structure
2. Classifies it — practice problems? lecture notes? syllabus? assignment?
3. Extracts individual problems or topics
4. Matches them to your existing tasks by subject
5. Adds them as completion goals
6. Shows them in your workspace when you start that task

Upload a syllabus? Jarvis extracts topics and deadlines and weaves them into your schedule.

**The honest comparison:** Motion's AI Docs assistant can extract action items from meeting notes and documents — that's real, and it's worth knowing. Notion AI can read PDFs and summarize them. But neither of them **connects extracted content to scheduled tasks and surfaces it in context when you start working**. Motion extracts tasks from docs. Jarvis extracts tasks, matches them to existing scheduled items by topic similarity, enriches the completion criteria, and shows the relevant problems in your workspace at the right moment.

**Current status:** The classification pipeline works. The full enrichment (extracting individual problems, matching to tasks, adding completion criteria) is in progress — the architecture is built and tested, the document handlers are being fleshed out.

---

### 7. Big Goals Become Small Wins

You say: *"Prepare for a deep learning competition by Friday."*

Jarvis returns:

| Task | Time | You're Done When... |
|------|------|---------------------|
| Study CNNs — convolution layers | 25 min | Can explain how a filter slides over an image |
| Study backpropagation math | 25 min | Can trace gradients through a 3-layer network |
| Practice: build a basic neural network | 25 min | Working code that trains on MNIST |
| Study optimization algorithms | 25 min | Can explain the difference between SGD and Adam |
| Mock contest: solve timed problems | 25 min | Completed 3+ problems under time pressure |

Each task includes:
- **Clear "done" criteria** — so you never wonder "am I actually done?"
- **A backup plan** — "if you feel stuck, try this instead" (so you don't just quit)
- **A difficulty score** — hard tasks scheduled during peak hours, easy ones when you're tired

**The honest comparison:** Motion chunks big tasks by time (splits a 10-hour task into 2-hour blocks). Trevor AI breaks tasks into 5 steps. Structured and Taskade do basic AI subtask generation. But none of them produce **completion criteria, contingency plans, or difficulty-weighted scheduling**. They split by time. Jarvis splits by concept, with science behind it.

The science: tasks are capped at 25 minutes based on Cognitive Load Theory (how much your brain can handle at once). Each task includes an "implementation intention" — a psychological technique where you pre-commit to what you'll do when you hit an obstacle. Research shows this improves goal completion rates by 20-40%.

---

### 8. Memory That Works Like Yours

Jarvis doesn't just remember facts. It builds a living, evolving picture of who you are:

- **Right now:** What you said today + the most relevant things from your entire history
- **Past conversations:** Summaries of every session — what you worked on, how you felt
- **Deep knowledge:** Your preferences, habits, patterns, goals, constraints

And here's what makes this different: **memories fade if you don't use them.** Told Jarvis you're a night owl 6 months ago but have been doing morning tasks fine ever since? That preference fades naturally. New habits replace old ones — just like human memory.

If a new preference contradicts an old one, the old memory isn't deleted — it's marked as superseded but kept for pattern analysis. ("User shifted from night owl to early bird in March.")

**The honest comparison:** No competitor has a memory system with natural decay. ChatGPT and Claude reset every conversation. Motion and Reclaim remember your settings but not your behavioral history. Notion remembers your workspace data but doesn't model memory strength, decay, or contradiction.

This is the part of Jarvis inspired by memory science — specifically the SuperMemo-2 algorithm used in flashcard apps like Anki, applied to AI memory itself. Memories that are reinforced by behavior persist. Memories that aren't accessed fade. It's simple, but no one else does it.

---

### 9. Gets Smarter Every Week

Today, Jarvis uses pattern detection and rules to learn from your behavior. The architecture is designed for a clear evolution:

**Coming next:**
- **Mastery tracking** — The system learns how well you understand each topic based on your task completions. Struggled with recursion? More practice scheduled. Aced arrays? Move on faster. Like a tutor with perfect memory.

- **Personalized task ordering** — Not just "earliest deadline first." The system learns what sequence of tasks *actually leads to the best outcomes for you* based on your completion history.

- **Energy prediction** — Learns your daily cognitive rhythm. Sharp at 2 PM, tired at 4? The schedule adjusts automatically.

**Why these aren't built yet (and why that's okay):** These features need real user data to work. Mastery tracking needs 100+ task completions to be meaningful. Personalized ordering needs months of history. Energy prediction needs weeks of daily patterns. We've designed them fully — the architecture, the data models, the integration points are all documented. We're waiting on the data, not the design.

**Why this matters:** Every week of use makes Jarvis smarter. After 3 months, switching to anything else would feel like starting over. That's not lock-in — it's that the AI genuinely knows you.

---

### 10. Ambient Life Intelligence — Jarvis Sees Your Life Happening

This is what makes Jarvis feel like the Iron Man version. Not a tool you open. **An intelligence running in the background — always watching, always understanding, always adapting.**

With your permission, Jarvis connects to the apps where your life actually happens — WhatsApp, Slack, Gmail, Calendar — and **understands** what's going on:

**Your girlfriend texts: "Surprise dinner tonight at 8!"**
Jarvis sees it. Your 7-9 PM study block? Already moved to tomorrow morning. You get a quiet nudge: *"Moved evening study to tomorrow 10 AM. Enjoy dinner."* You didn't open any app. It just happened.

**Your research professor pings on Slack: "Urgent meeting at 3 PM."**
Your 2-4 PM deep work tasks rearrange instantly — hard ones shift to your next focus window, a quick review fills the gap before 3. *"Professor meeting at 3. Moved DSA deep work to tomorrow."*

**Group project member on WhatsApp: "Deadline moved to Wednesday."**
Jarvis extracts the change. Project tasks spread out. The freed-up time goes to exam prep that needs it more.

**Email from university: "Exam schedule released."**
Jarvis reads the attachment, extracts dates, and adjusts your study plan across 3 weeks. *"Data Structures exam moved to April 18. Prep schedule adjusted."*

**The key:** This isn't "if calendar event detected, move tasks." Jarvis **understands context**:
- *"My mom's visiting this weekend"* → Saturday and Sunday are now social. Tasks redistribute to the week.
- *"Sprint review pushed to Friday"* → Friday afternoon is blocked. Prep moves earlier.
- *"Wanna grab coffee at 4?"* → Is 4 PM critical deep work or light tasks? If light, Jarvis makes room. If critical, it asks: *"4 PM is your deep work window. Move coffee to 5, or shift study to tomorrow?"*

**Why no competitor does this:** Every other tool — Motion, Reclaim, Notion — waits for you to update it. You have to go in, move things, add the meeting, reschedule manually. By the time you've reorganized, you've burned the mental energy that planning was supposed to save. Jarvis does it before you think about it. Life changes. Plan changes with it. Automatically.

**Privacy:** Only with explicit permission, per app. You choose what Jarvis sees. Content processed locally — not stored permanently. Disconnect any app instantly. Override any adjustment with one tap. Full transparency.

**Status:** Planned feature. The core intelligence supports it — brain dump extractor already handles unstructured text, `trigger_replan` already exists for real-time recalibration. What's needed: app integration layer (WhatsApp Business API, Slack API, Gmail API) and real-time message classification.

> *You live your life. Jarvis handles the logistics.*

---

## The Complete Competitive Landscape (Deep Research, 15+ Tools)

### Tier 1: Direct Competitors

| Tool | What It Does | What It Doesn't Do (That Jarvis Does) |
|------|-------------|---------------------------------------|
| **Motion** ($34/mo, $51M raised, YC W20) | Auto-schedules tasks, re-optimizes when meetings change, AI Docs extracts action items, learns patterns in ~2 weeks | No conceptual task decomposition with completion criteria. No memory that changes solver constraints. No schedule negotiation (auto-commits). No anti-guilt design. No psychology frameworks. Cloud-only. |
| **Reclaim.ai** (Acquired by Dropbox) | Protects focus time and habits, learns preferred habit times, smart meeting scheduling, 320K+ users | No task decomposition. No document intelligence. No persistent memory. No schedule negotiation. No anti-guilt. Cloud-only. |
| **Morgen** ($9/mo) | AI daily plan with preview-approve model, pads time estimates by 20%, splits long tasks by time | No behavioral learning. No document intelligence. No persistent memory. No anti-guilt. Cloud-only. **Closest on schedule negotiation.** |

### Tier 2: Adjacent Tools

| Tool | What It Does | Gap vs. Jarvis |
|------|-------------|----------------|
| **Sunsama** ($25/mo) | Guided morning planning ritual, manual timeboxing, calm design | Essentially zero AI. Manual-first philosophy. Reviewers note lack of AI as growing weakness in 2026. |
| **Notion AI** | Custom AI agents, PDF analysis, task extraction, workspace organization | Not a scheduler. Cannot auto-schedule tasks to calendar. Powerful platform, but you'd need to build your own scheduling engine on top. |
| **Akiflow** ($34/mo) | Universal task inbox from 20+ tools, "Replan Undone Tasks" feature | No deep intelligence. Task capture, not task preparation. |
| **Trevor AI** ($5/mo) | Breaks tasks into 5 steps, learns from routines, predicts durations | Lightweight. No document intelligence. No memory system. No constraint programming. |
| **Structured** | Beautiful visual timeline, AI subtasks, can scan physical timetables | No behavioral learning. No document pipeline. No constraint solver. |

### Tier 3: Different Category

| Tool | Why It's Not a Threat |
|------|----------------------|
| **Clockwise** | Dead. Acquired by Salesforce, shut down March 27, 2026. |
| **Blockit AI** ($1K/yr) | Enterprise meeting coordination. Different market entirely. |
| **Vela** (YC W26) | Multi-party scheduling (staffing, interviews). Enterprise, not personal. |
| **Taskade** | Team collaboration with AI agents. Has decomposition + vectors, but team-focused. |
| **Saner.AI** ($8-16/mo) | ADHD-friendly inbox scanner. Adjacent philosophy, no scheduling depth. |

---

## What's Genuinely Unique About Jarvis

After researching every tool, here's what **no one else combines**:

| Capability | Closest competitor | Why Jarvis goes deeper |
|---|---|---|
| Brain dump → full schedule | Motion (but needs pre-created tasks) | Jarvis starts from raw text, not structured input |
| Mathematical constraint solver | Motion (unknown algorithm) | Jarvis uses verifiable constraint programming — guarantees or honest "doesn't fit" |
| Task decomposition with completion criteria + contingency plans | Trevor AI (5 generic steps) | Jarvis decomposes by concept, adds "done when..." criteria and "if stuck, try..." plans |
| Behavioral learning → scheduling constraints | Motion (learns patterns), Reclaim (habit times) | Competitors adjust *where*. Jarvis adjusts *what, how much, and with what math* |
| Document → schedule pipeline | Motion AI Docs (extracts action items) | Jarvis classifies, extracts, matches to existing tasks, enriches criteria, surfaces in workspace |
| Schedule negotiation | Morgen (preview-approve) | Jarvis adds: reject with reason (builds memory), chat to modify, system learns from every interaction |
| Memory with natural decay | Nobody | Three-tier memory inspired by how human memory actually works |
| Anti-guilt as a system principle | Sunsama (calm vibes), Saner (ADHD-friendly) | Jarvis builds it into the scheduler's failure mode, not just the UI tone |
| Psychology frameworks (task design) | Nobody | Implementation intentions, cognitive load management, mastery orientation — in every task |
| Local-first / on-device | Nobody (in this category) | Core engine runs on Apple Silicon. Privacy by architecture. |
| Ambient life intelligence (reads your apps, auto-recalibrates) | Nobody | Girlfriend texts about dinner → evening study moves. Professor Slacks about a meeting → deep work rearranges. No other tool reacts to your life in real-time across communication channels. |

**The combination is the product.** Any single feature has partial overlap somewhere. The full stack — document intelligence feeding into psychologically-grounded task decomposition, scheduled by a constraint solver, adapted by behavioral learning that changes the solver's constraints, with memory that decays like human memory — exists nowhere else.

---

## "What If They Copy Us?"

This is the right question. Here's the honest answer:

### Why it's hard to copy

1. **The learning loop is architectural.** Behavior → memory → constraint → schedule is one integrated engine. In Motion or Reclaim, the scheduler is a separate system. To add behavioral learning that changes scheduling *constraints* (not just preferences), they'd need to rebuild their core. That's a multi-year project for a company with paying customers who expect stability.

2. **Document intelligence is a full pipeline.** "Add PDF upload" takes a week. Extract → classify → match to tasks → enrich criteria → surface in workspace → trigger replan is months of engineering that has to integrate with the scheduler.

3. **User data compounds.** After 6 months, Jarvis knows your energy patterns, topic strengths, editing habits, rejection reasons. That behavioral data can't be exported to another tool. Switching feels like losing a tutor who knows you deeply.

4. **Psychology is the foundation, not a feature.** You can't "add anti-guilt" to a product built around overdue notifications. It's a design philosophy that touches every screen, every message, every interaction.

5. **Ambient intelligence changes the product category.** Once Jarvis is connected to WhatsApp, Slack, and Gmail, it's not a tool you open — it's an intelligence that lives in your life. Motion reschedules when a *calendar event* changes. Jarvis reschedules when your *girlfriend texts about dinner*. That's a fundamentally different product.

### But let's be real

Motion has $51M and smart people. They **will** add some of these features over time. So the defense is:

- **Speed.** Ship the integrated version while they're still adding features one by one.
- **Depth.** They'll add surface-level behavioral learning. You'll have memory decay, pattern detection, and constraint bridge working as one system.
- **Focus.** They serve enterprise teams. You serve students and individual learners. Different users, different needs, different product.
- **Data moat.** Every day a user spends with Jarvis makes the system harder to replace. Not because of lock-in — because the AI genuinely knows them.

---

## How to Explain Jarvis in 30 Seconds

> "You know how in Iron Man, Jarvis isn't a tool Tony opens — it's always running, always thinking ahead, always handling things before Tony asks?
>
> That's what we're building. You dump everything on your mind and Jarvis builds your entire day. It breaks big goals into small steps, builds a schedule that can't have conflicts, reads your documents and connects them to your tasks, and learns from your behavior to get better every day.
>
> And it doesn't stop there. Connect your WhatsApp, Slack, Gmail — and Jarvis watches your life happen. Girlfriend texts about dinner? Your evening study block already moved. Professor pings about an urgent meeting? Your deep work is already rearranging. You don't open an app. Life changes, plan changes with it.
>
> It's not a calendar. It's not a to-do list. It's a personal intelligence that handles planning so you can focus on doing."

---

## How to Explain What Makes It Different

> "Lots of tools schedule. Motion does it, Reclaim does it. But scheduling is only one piece. No one else:
>
> - Breaks your goals into small, focused tasks with clear finish lines
> - Reads your documents and connects the right problems to the right tasks
> - Learns from what you skip, edit, and reject — and changes the actual scheduling math
> - Proposes a draft schedule you can negotiate with in plain English
> - Treats impossibility as a signal to adjust, not a reason to feel guilty
> - Watches your life across WhatsApp, Slack, and Gmail — and auto-recalibrates when things change, before you even think about it
>
> Each of these exists partially in some tool somewhere. The combination — where documents feed into tasks, tasks feed into schedules, schedules feed into behavior, behavior feeds back into everything, and your real life feeds in through your communication apps — that's what nobody else has built."

---

## The 60-Second Pitch

*"86% of students can't manage their time. 82% of professionals don't even have a system. And the average person is productive for less than 3 hours a day.*

*The problem isn't motivation — it's that planning is exhausting. And every tool on the market still makes you the planner.*

*We built Jarvis — an AI Preparation Engine. You dump everything on your mind — goals, deadlines, habits, documents — and Jarvis handles the rest.*

*It breaks your goals into 25-minute tasks, each with a clear finish line and a backup plan for when you get stuck. It builds a schedule using constraint programming — the same math used in airline scheduling — so conflicts are literally impossible. It reads your documents and connects the right practice problems to the right tasks. And it learns from your behavior — what you skip, what you edit, what you reject — and changes the scheduling math to fit how you actually work.*

*But here's where it gets different. Connect your WhatsApp, Slack, Gmail — and Jarvis watches your life happen. Girlfriend texts about surprise dinner? Jarvis already moved your evening study to tomorrow. Professor pings about an urgent meeting? Your deep work is already rearranging. You don't open an app. You don't move blocks. Life happens, and your plan adapts — before you even think about it.*

*$13.6 billion market. Motion proved the category at $550M. But Motion reschedules when a calendar event changes. Jarvis reschedules when your girlfriend texts. That's not a scheduling tool. That's a personal intelligence.*

*Working prototype. 113 tests passing. And a learning loop that compounds daily.*

*We're building the Jarvis from Iron Man — for real life."*

---

## Handling Tough Questions

### "Why not just use ChatGPT?"

"ChatGPT is great at understanding language. But ask it to plan your day twice — you'll get two different answers. It might schedule two things at the same time. It forgets everything when the conversation ends. We use AI where it's strong — understanding your words, breaking down goals, synthesizing responses. But the schedule? That's built by a constraint solver that physically can't produce a broken plan. And it remembers everything about how you work — permanently."

### "Motion has $51M and $50M ARR. How do you compete?"

"Motion proved this is a massive market — that's great for us. But Motion schedules *when*. We handle *what, when, how, and with what materials.* Motion can't decompose your goals into psychologically-grounded micro-tasks, can't read your syllabus and connect problems to tasks, can't negotiate your schedule in conversation, and doesn't have a memory system that changes the scheduling math based on your behavior. We're not a better calendar. We're a different product category."

### "Morgen already has schedule negotiation"

"They do — preview and approve. That's real competition on that specific feature. Our negotiation goes deeper: reject with a reason and the system permanently learns from it. Chat in natural language to modify. Every interaction builds memory that improves future schedules. It's not just preview-approve — it's a learning conversation."

### "Motion's AI Docs can extract tasks from documents too"

"Motion extracts action items from meeting notes — that's genuine. Our pipeline goes further: we classify the document type (syllabus vs. practice problems vs. lecture notes), extract structure accordingly, match extracted content to existing scheduled tasks by topic similarity, enrich the completion criteria, and surface the right materials in your workspace when you start working. It's not just extraction — it's integration with the schedule."

### "What actually works today?"

"The full pipeline: brain dump → task decomposition → conflict-free scheduling → document classification → behavioral pattern detection → memory with natural decay → constraint bridge that turns behavior into scheduling rules → draft negotiation → habit tracking. 113 tests passing. The things that are planned — mastery tracking, personalized ordering, full offline mode — need user data we don't have yet. The architecture for them is designed and documented. We're waiting on data, not design."

### "Isn't connecting to WhatsApp and Slack a privacy concern?"

"Only with explicit permission per app. You choose what Jarvis can see. Content is processed locally — we don't store your messages on a server. Jarvis reads for scheduling signals: is this a new plan, a deadline change, a social commitment? Content isn't kept. Disconnect any app instantly, undo any adjustment with one tap. Full transparency on what it read and why."

### "What if someone builds this on top of Notion's AI Agents?"

"They could approximate parts of it. But constraint programming, behavioral pattern detection feeding into a mathematical solver, memory with decay, on-device AI processing, and ambient life intelligence across WhatsApp/Slack/Gmail aren't things you wire up with Notion automations. The analytical engine is the moat."

### "What's your moat, really?"

"Five things. Architecture — behavior changes memory, memory changes constraints, constraints change the schedule. One integrated engine. Data — after 6 months, Jarvis knows a user deeply. Personalization you can't export. Psychology — anti-guilt, mastery orientation, cognitive load management. Foundation, not features. Ambient intelligence — once Jarvis is connected to your WhatsApp, Slack, and Gmail, it's not a tool you open, it's an intelligence living in your life. Motion reschedules when a calendar event changes. Jarvis reschedules when your girlfriend texts about dinner. And compounding — every day of use makes the system harder to replace."

---

## The Market

| Stat | Number |
|------|--------|
| AI productivity tools market (2025) | **$13.6 Billion** |
| Market growth | **25% CAGR** |
| Motion valuation | **$550M** (proves the category) |
| Reclaim.ai | **Acquired by Dropbox** (proves exit potential) |
| Students struggling with time management | **86%** |
| Professionals with no system | **82%** |
| Average productive time per day | **2 hours 53 minutes** |
| Improvement from psychology frameworks we use | **20-40%** (peer-reviewed) |
| Likelihood to graduate on time with structured time management | **1.7x higher** |

---

## Who It's For

**Students first.** They have the most complex scheduling needs (multiple courses, exams, projects, habits, social life) and the least tolerance for manual planning. They also produce the highest-value behavioral data for training the learning systems.

**Then professionals who learn.** Managing work deadlines while upskilling, staying prepared for meetings, maintaining habits. Too many balls in the air for any calendar to handle.

**Then anyone overwhelmed.** ADHD. Executive function challenges. Packed lives. The people who need a system most but have the hardest time maintaining one.

---

## Business Model

| Tier | Price | What You Get |
|------|-------|-------------|
| **Free** | $0 | 1 goal, basic scheduling, habit tracking |
| **Pro** | $9.99/mo | Unlimited goals, document intelligence, behavioral learning, draft negotiation |
| **Team** | TBD | Shared scheduling, team insights |

**Unit economics:** Cloud AI costs ~$0.001/request. At 1,000 daily active users = ~$90/month. Local-first architecture drives this lower as on-device models improve.

---

## Sources (All Fact-Checked)

| Claim | Source | Status |
|---|---|---|
| 86% students + time management | Judkin 2024 | Verified |
| 82% no system | LifeHack Method 2025 | Verified |
| 2h 53m productive time | LifeHack Method / VoucherCloud | Verified |
| 80-95% procrastination | Judkin 2024 | Verified |
| $13.6B market, 25% CAGR | Business Research Insights | Verified |
| Motion $50M ARR, $550M valuation | Sacra, TechCrunch | Verified |
| Motion raised $51M+ | TechCrunch Sept 2025 | Verified |
| Reclaim acquired by Dropbox | Crunchbase | Verified |
| Clockwise shut down March 27, 2026 | Clockwise website | Verified |
| Morgen has preview-approve scheduling | Morgen.so product page | Verified |
| Motion AI Docs extracts action items | Motion features page | Verified |
| Sunsama "essentially no AI" | Saner.AI review, 2026 | Verified |
| Trevor AI 5-step decomposition | Trevor.ai product page | Verified |
| 20-40% improvement (WOOP) | Oettingen et al. (peer-reviewed) | Verified |
| Motion's exact algorithm | NOT public | Honest gap |
| Reclaim's exact algorithm | "Patent-pending" | Honest gap |

---

*Internal pitch prep. All competitor claims based on public information verified March 2026. Where we don't know, we say so. No false claims about competitors.*
