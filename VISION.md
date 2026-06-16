## Automatiq

Automatiq began in a classroom. I was watching one of my teachers manually fill out forms for an online event, painstakingly copy-pasting data row by row from an 500 row Excel sheet. As a developer, watching manual data entry was painful. I thought, *"I can write a script for this."*

But when I looked for solutions to help generate the automation quickly, I hit a wall. Nearly every existing solution in the market wanted to spin up a heavy Chromium browser, or relied on flaky browser extensions to click buttons and drag sliders on a screen.

In my years working as an Automation freelancer, I learned a simple truth: **UI automation is inherently brittle.** Buttons move, pages load slowly, and UI quirks break scripts. Meanwhile, under the hood, websites are just sending simple text-based HTTP requests. Browser-based automation/scraper workflows can take up a lot of memory and compute to run. They deplete resources, they are slower than requests-based scripts.

Why had the industry accepted bloated browser-based solutions as the standard, when a clean, 300-line Python script making direct requests is hundreds of times faster, and nearly 10 times lighter?

> The answer: Reverse-engineering websites are a complex, time-consuming challenge and reverse-engineered scripts break all the time.

Personally, It takes me about an hour or 2 to come-up with an MVP manually that can get at least one data point or automate one single automation workflow.
Fixing broken scripts requires you to go through the site again and patch your script up manually, taking about 75%-50% the same time as writing them the first time. And yes, scrapers can break a lot (But not as much as the UI-based automations)

The Second issue is the Myth that requests-based solutions won't work anymore. But my research on that showed otherwise:

|Tier|What it includes|Estimated share of sites|Source|
|:-|:-|:-|:-|
|**None** (no protection)|No CAPTCHA, no fingerprinting, no rate limit, no WAF, no anti-bot vendor|**\~60–62%**|[DataDome 2025 Global Bot Security Report](https://datadome.co/resources/bot-security-report/)|
|**Light** (CSRF, headers, basic rate limiting, simple obfuscation)|Mostly app-framework defaults; no dedicated anti-bot product|**\~25–35%** (subset of "partially protected")|[DataDome 2025 Report](https://datadome.co/resources/bot-security-report/) \+ [W3Techs Cloudflare](https://w3techs.com/technologies/details/cn-cloudflare)|
|**Medium** (TLS/JA3 checks, image CAPTCHA, reCAPTCHA v2, basic WAF)|reCAPTCHA, hCaptcha, Cloudflare WAF on free plan|**\~10–15%**|[BuiltWith reCAPTCHA v3](https://trends.builtwith.com/widgets/reCAPTCHA-v3) \+ [BuiltWith hCaptcha](https://trends.builtwith.com/widgets/hCaptcha)|
|**Hard** (reCAPTCHA v3, Turnstile, hCaptcha Enterprise, Cloudflare Bot Mgmt)|Vendor-managed bot mitigation, behavioral scoring|**\~3–5%** of all sites; **\~20–30%** of top-ranked sites|[Cloudflare Bot Mgmt market share (wmtips)](https://www.wmtips.com/technologies/bot-mitigation/cloudflare-bot-management/) \+ [BuiltWith reCAPTCHA Enterprise](https://trends.builtwith.com/widgets/reCAPTCHA-Enterprise)|
|**Very Hard** (Akamai Bot Manager, DataDome, HUMAN/PerimeterX, Kasada, Imperva, full canvas/WebGL+TLS fingerprinting)|Enterprise anti-bot stack with multi-signal fingerprinting|**\~1–3%** of all sites; concentrated among Fortune-500 / e-commerce / travel / financial|[BuiltWith Akamai Bot Manager](https://trends.builtwith.com/widgets/Akamai-Bot-Manager) \+ [BuiltWith DataDome](https://trends.builtwith.com/widgets/DataDome) \+ [UCSD IMC 2025 canvas fingerprinting paper](https://www.sysnet.ucsd.edu/~voelker/pubs/canvas-imc25.pdf)|

Just basic requests-based solution (with TLS spoofing mechanisms) can be applied up to 70% to 90% of the current websites.

All I had to do was to make the requests-based approach easier to create and maintain for everyone

Automatiq was born to solve this problem, making the process of Reverse-engineering as easy and autonomous as possible.

## How Automatiq Works

Standard coding agents (like Claude Code or OpenCode) are designed to write clean code in a local directory, then write more than they read. Most of the knowledge they require was provided in their training data.

But a reverse-engineering agent is entirely different: it needs to get its hands dirty with complex, broken, unformatted data from decade-old websites. It must read more than it writes, and most of the knowledge required isn't available in their training data.

Getting an AI to successfully reverse-engineer web requests required 6 months of time, built through a lot of trials and errors, with expensive API bills. I had to rewrite the whole module four times, due to logical mistakes I had made. But finally, I am presenting here.

### Phase 1: The Recorder

The First question was **How?**, Usually developers use Devtool's network tab to see all the requests sent. This was pretty easy to implement, all I had to do was to record the requests via Chrome Devtools Protocol(CDP).
Each request is converted into a folder, Headers, payload data, response data as separate files, and their extension generated by Magika module by analyzing the file content.

If you just hand an AI a pile of raw network requests, it gets lost. It doesn't know *why* a request was made. Academic papers on UI automation suggest generating "high-level summaries" using screenshots of what the user is doing. But a single screenshot doesn't capture intent.

To solve this without racking up massive API costs by feeding the Vision-Language Model (VLM) full videos, Automatiq captures **action-correlated clips**. It records a 4-second, ultra-low-framerate video chunk precisely when an action (like a click or keystroke) occurs. The AI sees what happened before, during, and after the action. This transforms user and browser activity into an easily navigable file system that gives the agent a clear compass for its reverse-engineering task.

Because this output is just a cleanly structured file directory, there is **zero vendor lock-in**. You don't have to use Automatiq's built-in agent, you can point your own custom LLMs or scripts at the recorded folder and let them work.

### Phase 2: The Agent

Building the agent itself took three complete iterations. I initially used a standard Python shell, but calling crucial command-line tools like `jq` or `ripgrep` was clunky and error-prone. Then I moved to Xonsh, a shell that mixes Python and Bash. It worked better, but smaller, cost-effective LLMs failed miserably... they simply didn't have enough "intrinsic knowledge" of Xonsh in their training data, after all, it is an unfamiliar tool.

That's when I started again, from scratch: **What environment do LLMs know best?** They are usually trained of Jupyter Notebooks. I rebuilt the agent from scratch using **IPython** (the underlying Python REPL for Jupyter). Suddenly, even the least expensive LLMs performed exceptionally well, because they were operating in an environment they natively understood.

To handle the Obvious problem of handling huge files and outputs, I had used IPython's Cell system to advantage. Every tool output gets removed from history, after 20 turns (like a sliding window), and can be accessed with a Ipython magic command. I had also set a 10KB output + pagination system to maintain the context.

Finally, to ensure cross-platform compatibility without losing standard terminal tools, Automatiq bundles a tiny (< 1 MB) instance of BusyBox for Windows. Now, the agent can seamlessly pivot between Python and Bash commands anywhere, extracting the exact tokens and APIs it needs.

### The Goal

Automatiq is built to make it easier to create automation and webscraping scripts, for the developers, as well as people stuck in repetitive manual data entry work. Just like people say they can build anything with Claude Code, Automatiq is designed to allow you to extract or automate nearly anything on the web.

## The Roadmap and Future Plans for Automatiq

### JavaScript Virtual Machine

Many modern sites use obfuscated JavaScript to generate request credentials or cryptographic tokens. Instead of firing up Chromium to run this, Automatiq can integrate a small JS VM, exactly how tools like `yt-dlp` run YouTube's JS natively to generate request credentials. The AI agent will extract the specific JavaScript it needs, attach the VM as a Python module, and use it in the final script to generate required tokens seamlessly.

### Surgical Browser Usage

If a site's fingerprinting is truly unbreakable, the generated script *still* shouldn't run a headless browser for the entire process. The AI should learn to spin up Chrome just for the exact request that needs it, grab the clearance token, kill the browser immediately, and handle the rest of the workflow in pure Python.

### Isolated Technique Plugins

To bypass complex defenses like Cloudflare or advanced CAPTCHA, Automatiq will introduce a plugin system. We have no interest in building a marketplace for "Instagram bots" or "LinkedIn scrapers." Instead, these plugins will be isolated techniques, similar to cybersecurity skills like TLS spoofing, JS debugging hooks, or CAPTCHA routing. They'll give the AI the exact tools it needs to break a specific defense and rebuild the script without cluttering the core engine.

## Automatiq is a Community Project

I built Automatiq because life is too short to do things the long way, I hate inefficient solutions that steal people's time. We break the web down so we can automate it efficiently, making development faster, cheaper, and easier for everyone.

I am also looking for collaborators on this project as two minds are better than one, and it will be helpful to grow the project, and manage the demand.

Whether you want to improve the recorder or the agent, or just testing it out on your hardest targets, contributions of any kind are welcome. I have meant to keep Automatiq as a free opensource community project forever and ever. Thank you for being here.
