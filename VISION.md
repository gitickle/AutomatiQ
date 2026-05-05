<div style="max-width: 680px; font-family: var(--font-sans); color: var(--color-text-primary); line-height: 1.75; padding: 1.5rem 0;">

  <h1 style="font-size: 22px; font-weight: 500; margin: 0 0 0.5rem;">The Vision of Automatiq</h1>
  <p style="margin: 0 0 1rem; color: var(--color-text-secondary); font-size: 15px;">
    Automatiq began in a classroom. I was watching one of my teachers manually fill out forms for an event, painstakingly copy-pasting data row by row from an Excel sheet. As a developer, watching manual data entry was painful. I thought, <em>"I can write a script for this."</em>
  </p>
  <p style="margin: 0 0 1rem; color: var(--color-text-secondary); font-size: 15px;">
    But when I looked for modern AI tools to help generate the automation quickly, I hit a wall. Every existing solution wanted to spin up a heavy, headless Chromium browser, or relied on flaky browser extensions to click buttons and drag sliders on a screen.
  </p>
  <p style="margin: 0 0 1rem; color: var(--color-text-secondary); font-size: 15px;">
    In my years working as an Automation freelancer, I learned a simple truth: <strong>UI automation is inherently brittle.</strong> Buttons move, pages load slowly, and UI quirks break scripts. Meanwhile, under the hood, websites are just sending simple text-based HTTP requests. Why had the industry accepted bloated 2GB browser automation as the standard, when a clean, 300-line Python script making direct requests is hundreds of times faster and infinitely more stable?
  </p>
  <p style="margin: 0 0 2rem; color: var(--color-text-secondary); font-size: 15px;">
    The answer: because reverse-engineering those requests is a complex, time-consuming challenge. Automatiq was born to solve exactly that. It is a rebellion against bloated web automation.
  </p>

  <h2 style="font-size: 18px; font-weight: 500; margin: 0 0 1.25rem;">How Automatiq Works: A Journey of Iteration</h2>
  <p style="margin: 0 0 1.5rem; color: var(--color-text-secondary); font-size: 15px;">
    Standard coding agents (like Claude Code or OpenCode) are designed to write clean code in a local directory. But a reverse-engineering agent is entirely different: it needs to get its hands dirty with complex, broken, unformatted data from decade-old websites. It must read more than it writes.
  </p>
  <p style="margin: 0 0 1.5rem; color: var(--color-text-secondary); font-size: 15px;">
    Getting an AI to successfully reverse-engineer web requests required two distinct phases, built through a lot of trial, error, and expensive API bills.
  </p>

  <div style="border-left: 2px solid var(--color-border-secondary); padding-left: 1.25rem; margin-bottom: 1.75rem;">
    <h3 style="font-size: 15px; font-weight: 500; margin: 0 0 0.5rem;">Phase 1: The Recorder (Capturing User's Intent)</h3>
    <p style="margin: 0 0 0.75rem; color: var(--color-text-secondary); font-size: 15px;">
      If you just hand an AI a pile of raw network requests, it gets lost. It doesn't know <em>why</em> a request was made. Academic papers on UI automation suggest generating "high-level summaries" using screenshots of what the user is doing. But a single screenshot doesn't capture intent.
    </p>
    <p style="margin: 0 0 0.75rem; color: var(--color-text-secondary); font-size: 15px;">
      To solve this without racking up massive API costs by feeding the Vision-Language Model (VLM) full videos, Automatiq captures <strong>action-correlated clips</strong>. It records a 4-second, ultra-low-framerate video chunk precisely when an action (like a click or keystroke) occurs. The AI sees what happened before, during, and after the action. This transforms user and browser activity into an easily navigable file system that gives the agent a clear compass for its reverse-engineering task.
    </p>
    <p style="margin: 0; color: var(--color-text-secondary); font-size: 15px;">
      Because this output is just a cleanly structured file directory, there is <strong>zero vendor lock-in</strong>. You don't have to use Automatiq's built-in agent—you can point your own custom LLMs or scripts at the recorded folder and let them work.
    </p>
  </div>

  <div style="border-left: 2px solid var(--color-border-secondary); padding-left: 1.25rem; margin-bottom: 2.5rem;">
    <h3 style="font-size: 15px; font-weight: 500; margin: 0 0 0.5rem;">Phase 2: The Agent (Understanding LLM's Native Language)</h3>
    <p style="margin: 0 0 0.75rem; color: var(--color-text-secondary); font-size: 15px;">
      Building the agent itself took three complete iterations. I initially used a standard Python shell, but calling crucial command-line tools like <code>jq</code> or <code>ripgrep</code> was clunky and error-prone. Then I moved to Xonsh, a shell that mixes Python and Bash. It worked better, but smaller, cost-effective LLMs failed miserably... they simply didn't have enough "intrinsic knowledge" of Xonsh in their training data, after all its a unfamiliar tool.
    </p>
    <p style="margin: 0 0 0.75rem; color: var(--color-text-secondary); font-size: 15px;">
      That's when I started again, from scratch: <strong>What environment do LLMs know best?</strong> They are trained on billions of lines of Jupyter Notebooks. I rebuilt the agent from scratch using <strong>IPython</strong> (the underlying Python REPL for Jupyter). Suddenly, even the cheapest LLMs performed exceptionally well, because they were operating in an environment they natively understood.
    </p>
    <p style="margin: 0; color: var(--color-text-secondary); font-size: 15px;">
      Finally, to ensure cross-platform compatibility without losing standard terminal tools, Automatiq bundles a tiny (< 1 MB) instance of BusyBox for Windows. Now, the agent can seamlessly pivot between Python and Bash commands anywhere, extracting the exact tokens and APIs it needs.
    </p>
  </div>

  <div style="background: var(--color-background-secondary); border-radius: var(--border-radius-lg); padding: 1.25rem 1.5rem; margin-bottom: 2.5rem;">
    <h3 style="font-size: 15px; font-weight: 500; margin: 0 0 0.5rem;">The Goal</h3>
    <p style="margin: 0; color: var(--color-text-secondary); font-size: 15px;">
      Automatiq is built to break a website's security model apart, analyze the raw network requests with AI, and build it back up into a clean, entirely browser-less script. Just like people say they can build anything with Claude Code, Automatiq is designed to give you the ability to extract or automate anything on the web, completely without the bloat.
    </p>
  </div>

  <h2 style="font-size: 18px; font-weight: 500; margin: 0 0 1.5rem;">The Roadmap</h2>

  <div style="display: flex; flex-direction: column; gap: 1.25rem; margin-bottom: 2.5rem;">
    <div style="background: var(--color-background-primary); border: 0.5px solid var(--color-border-tertiary); border-radius: var(--border-radius-lg); padding: 1.25rem 1.5rem;">
      <h3 style="font-size: 15px; font-weight: 500; margin: 0 0 0.5rem;">JavaScript Virtual Machine</h3>
      <p style="margin: 0; color: var(--color-text-secondary); font-size: 15px;">
        Many modern sites use obfuscated JavaScript to generate request credentials or cryptographic tokens. Instead of firing up Chromium to run this, Automatiq will integrate a small JS VM, exactly how tools like <code style="font-family: var(--font-mono); font-size: 13px; background: var(--color-background-secondary); padding: 2px 5px; border-radius: 4px;">yt-dlp</code> run YouTube's JS natively to generate request credentials. The AI agent will extract the specific JavaScript it needs, attach the VM as a Python module, and use it in the final script to generate required tokens seamlessly.
      </p>
    </div>

  <div style="background: var(--color-background-primary); border: 0.5px solid var(--color-border-tertiary); border-radius: var(--border-radius-lg); padding: 1.25rem 1.5rem;">
      <h3 style="font-size: 15px; font-weight: 500; margin: 0 0 0.5rem;">Surgical Browser Usage</h3>
      <p style="margin: 0; color: var(--color-text-secondary); font-size: 15px;">
        If a site's fingerprinting is truly unbreakable, the generated script <em>still</em> shouldn't run a headless browser for the entire process. The AI will learn to spin up Chrome just for the exact request that needs it, grab the clearance token, kill the browser immediately, and handle the rest of the workflow in pure Python.
      </p>
    </div>

  <div style="background: var(--color-background-primary); border: 0.5px solid var(--color-border-tertiary); border-radius: var(--border-radius-lg); padding: 1.25rem 1.5rem;">
      <h3 style="font-size: 15px; font-weight: 500; margin: 0 0 0.5rem;">Isolated Technique Plugins</h3>
      <p style="margin: 0; color: var(--color-text-secondary); font-size: 15px;">
        To bypass complex defenses like Cloudflare or advanced CAPTCHAs, Automatiq will introduce a plugin system. We have no interest in building a marketplace for "Instagram bots" or "LinkedIn scrapers". Instead, these plugins will be isolated techniques, similar to cybersecurity skills like TLS spoofing, JS debugging hooks, or CAPTCHA routing. They'll give the AI the exact tools it needs to break a specific defense and rebuild the script without cluttering the core engine.
      </p>
    </div>
  </div>

  <div style="background: var(--color-background-secondary); border-radius: var(--border-radius-lg); padding: 1.25rem 1.5rem; margin-top: 1.25rem;">
    <h2 style="margin: 0 0 0.5rem; color: var(--color-text-primary); font-size: 15px; font-weight: 500;">Automatiq is a Community Project</h2>
    <p style="margin: 0 0 0.75rem; color: var(--color-text-secondary); font-size: 15px;">
      I built Automatiq because life is too short to do things the long way, and compute is too precious to waste on spinning up full browsers just to send an HTTP request. We break the web down so we can automate it efficiently, making development faster, cheaper, and easier for everyone.
    </p>
    <p style="margin: 0; color: var(--color-text-secondary); font-size: 15px;">
      Whether you want to improve the IPython agent, add a new technique plugin, or just test it out on your hardest scraping targets, contributions of any kind are welcome.

Thanks for being here. Let's shear away the bloat and make web automation fast, clean, exactly as it was always meant to be.
    </p>
  </div>

</div>
