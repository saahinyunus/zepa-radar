# Global Freelance & B2B Lead Radar: find clients who are hiring right now

**Stop scrolling Reddit and job boards. Get a clean list of real clients who want to pay a freelancer, each one scored by AI and delivered with a ready-to-send pitch.**

Freelancers and small agencies lose hours every week sorting through "for hire" spam, tutorials and vague posts to find a handful of genuine requests. This Actor does that sorting for you. It scans public hiring posts, throws away freelancers advertising themselves, scores the real opportunities from 0 to 100 and writes a short, human-sounding message you can send straight to the client.

**You only pay for qualified leads that land in your dataset.**

---

## What you get for every lead

- **Quality score (0-100)** based on how clear the scope is, whether a budget is stated, how urgent the work is and how credible the client looks
- **Category**: Software, Design, Video or Marketing & Writing
- **Estimated budget**: the stated budget or rate, or a clearly marked estimate
- **Client pain point** in one sentence
- **Two-sentence summary** of scope, skills and timeline
- **Ready-to-send pitch**: a 3-4 sentence message that opens with a specific detail from the client's own post
- **Direct link** to the original post so you can apply immediately

## Who it is for

| You are... | Use it to... |
| :--- | :--- |
| A freelance developer, designer, video editor or writer | Fill your pipeline with fresh, relevant requests every day |
| A small agency or studio | Find inbound-style leads without paying for ads |
| A lead-generation or sales team | Build lists of businesses that have a problem and a budget |
| A founder validating a service | See exactly what people are asking to be built or designed |

## Where the leads come from

- **Reddit**: dozens of communities where people post jobs and project requests, from r/forhire and r/DesignJobs to startup, SaaS, marketing and creator communities
- **Hacker News**: hiring and "looking for" posts and comments
- **Remote job boards**: RemoteOK, Remotive, Jobicy, Arbeitnow and We Work Remotely

All sources are public. No logins, no proxies and no private data.

## Categories

| Category | Typical requests |
| :--- | :--- |
| `SOFTWARE` | Web and mobile apps, APIs, SaaS, AI and automation, WordPress and Shopify work |
| `DESIGN` | Logos, branding, UI/UX, thumbnails, illustration, 3D |
| `VIDEO` | Video editing, Shorts/Reels/TikTok editing, motion graphics, VFX |
| `MARKETING_WRITING` | Copywriting, SEO, social media, ads, email marketing, growth |

## How to use it

1. Click **Try for free** and choose the categories you work in.
2. Set the **minimum quality score** (70 is a good starting point) and the **maximum number of leads**.
3. Optionally add **custom keywords** such as `shopify`, `react native` or `youtube shorts` to narrow the results.
4. Press **Start**. Results appear in the dataset as they are found.
5. Open the **Leads** view, pick a lead, copy the pitch and click the link to apply.

## Input

| Field | Type | Default | Description |
| :--- | :--- | :--- | :--- |
| `categories` | array | all four | Which kinds of work you want leads for |
| `min_quality_score` | integer | `70` | Only leads at or above this AI score are delivered |
| `max_items` | integer | `50` | The run stops after this many qualified leads |
| `custom_keywords` | array | none | Keep only posts that mention at least one keyword |
| `max_age_hours` | integer | `24` | Only posts from the last N hours (max 168) |
| `sources` | array | all | `reddit`, `hackernews`, `jobboards` |

Example:

```json
{
    "categories": ["SOFTWARE", "VIDEO"],
    "min_quality_score": 75,
    "max_items": 30,
    "custom_keywords": ["shopify", "shorts"],
    "max_age_hours": 48
}
```

## Output example

```json
{
    "id": "reddit:t3_1abcxyz",
    "source": "reddit",
    "category": "VIDEO",
    "title": "[HIRING] Short-form editor for a weekly podcast, $40 per clip",
    "url": "https://www.reddit.com/r/hireaneditor/comments/1abcxyz/hiring_shortform_editor/",
    "qualityScore": 88,
    "estimatedBudget": "$40 per clip",
    "clientPainPoint": "The host has no time to cut weekly podcast episodes into Shorts and Reels.",
    "summary": "A podcast host needs weekly 30-60 second vertical clips cut from long episodes. Premiere Pro experience and a 48-hour turnaround are required.",
    "winningPitch": "Hi there, I saw your post about turning your weekly podcast into Shorts and Reels. I can deliver captioned vertical clips within 48 hours of each episode and I'm happy to start with a free sample clip so you can check the style. Which episode would you like me to try first?",
    "postedAt": "2026-01-15T09:42:10+00:00",
    "scrapedAt": "2026-01-15T10:05:31+00:00"
}
```

Download the dataset as JSON, CSV, Excel, XML or HTML, or read it through the Apify API.

## Pricing

This Actor uses **pay-per-event** pricing: you are charged one small fixed amount for each qualified lead delivered to your dataset (see the Pricing tab for the current price). Posts that are filtered out, rejected by the AI or below your minimum score are never charged. Set the maximum cost per run in Apify and the Actor stops when it is reached.

## Run it on a schedule and connect it to your tools

- Use **Apify Schedules** to run it every morning, or every few hours.
- Send new leads to Google Sheets, Slack, Airtable, Notion or your CRM with the Apify integrations, Make, Zapier or n8n.
- Call it from your own code with the Apify API and clients for JavaScript and Python.

## Tips for better results

- Start with a minimum score of 70, then lower it to 55-60 if you want more volume.
- Use `max_age_hours` of 6-12 when you run it several times a day, so you only see fresh posts.
- Add two or three precise keywords for your niche; it is the fastest way to remove noise.
- Reply fast. Clients usually choose from the first handful of applicants.

## FAQ

**Does it find jobs for the whole world?**
Yes. Posts are collected from international communities and boards. Pitches are written in English.

**Are these real clients?**
The AI removes freelancers advertising their services, job seekers, discussion posts and obvious scams, and it only delivers posts from people or companies that appear to be hiring. No automated filter is perfect, so always read the original post before you apply.

**Does it contact anyone for me?**
No. It only finds and prepares leads. You decide who to contact and what to send.

**Is it legal?**
It reads publicly available posts and listings only. You are responsible for using the data in line with the terms of each source and with local laws, including anti-spam and data-protection rules, when you contact leads.

## Feedback and support

Found a bug or want another source or category? Open an issue in the **Issues** tab. Feature requests are welcome and are read.
