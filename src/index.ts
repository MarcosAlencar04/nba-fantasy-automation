import { DurableObject } from "cloudflare:workers";

const ESPN_SCOREBOARD =
  "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard";
const NBA_SCOREBOARD =
  "https://cdn.nba.com/static/json/liveData/scoreboard/todaysScoreboard_00.json";
const CONTAINER_PORT = 8080;
const PRE_GAME_LEAD_MS = 30 * 60 * 1000;
const MIN_LEAD_MS = 60 * 1000;

type Mode = "morning" | "pre-game" | "manual";

interface Env {
  RUNNER: DurableObjectNamespace<FantasyRunner>;
  RUN_TOKEN: string;
  ESPN_LEAGUE_ID?: string;
  ESPN_SWID?: string;
  ESPN_S2?: string;
  TELEGRAM_TOKEN?: string;
  TELEGRAM_CHAT_ID?: string;
  SMTP_HOST?: string;
  SMTP_PORT?: string;
  SMTP_USER?: string;
  SMTP_PASS?: string;
  EMAIL_FROM?: string;
  EMAIL_TO?: string;
  ADDS_USED?: string;
}

const PASSTHROUGH = [
  "ESPN_LEAGUE_ID",
  "ESPN_SWID",
  "ESPN_S2",
  "TELEGRAM_TOKEN",
  "TELEGRAM_CHAT_ID",
  "SMTP_HOST",
  "SMTP_PORT",
  "SMTP_USER",
  "SMTP_PASS",
  "EMAIL_FROM",
  "EMAIL_TO",
  "ADDS_USED",
] as const;

function childEnv(env: Env): Record<string, string> {
  const out: Record<string, string> = {};
  for (const key of PASSTHROUGH) {
    const value = env[key];
    if (typeof value === "string" && value.trim() !== "") out[key] = value.trim();
  }
  return out;
}

function etDateKey(date = new Date()): string {
  return new Intl.DateTimeFormat("en-CA", {
    timeZone: "America/New_York",
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
  }).format(date);
}

export class FantasyRunner extends DurableObject<Env> {
  private starting: Promise<void> | undefined;

  async fetch(request: Request): Promise<Response> {
    const url = new URL(request.url);
    if (url.pathname !== "/run") return new Response("not found", { status: 404 });
    const mode = (url.searchParams.get("mode") ?? "manual") as Mode;
    const notify = url.searchParams.get("notify") !== "0";
    return this.run(mode, notify);
  }

  async run(mode: Mode, notify = true): Promise<Response> {
    const started = Date.now();
    try {
      await this.ensureContainer();
      const res = await this.ctx.container!.getTcpPort(CONTAINER_PORT).fetch(
        new Request(`http://container/run${notify ? "" : "?dry=1"}`, { method: "POST" }),
      );
      const body = await res.text();
      await this.ctx.storage.put(`last:${mode}`, { at: started, status: res.status });
      if (mode !== "manual") await this.schedulePreGame();
      return new Response(body, {
        status: res.status,
        headers: { "Content-Type": "application/json; charset=utf-8" },
      });
    } catch (error) {
      return new Response(
        JSON.stringify({ ok: false, error: String(error), mode }),
        { status: 500, headers: { "Content-Type": "application/json; charset=utf-8" } },
      );
    }
  }

  async alarm(): Promise<void> {
    const today = etDateKey();
    const done = await this.ctx.storage.get<number>(`pre:${today}`);
    if (done === undefined) {
      await this.run("pre-game", true);
      await this.ctx.storage.put(`pre:${today}`, Date.now());
    }
  }

  /** Agenda o alarme para 30 min antes do primeiro jogo do dia (fuso da NBA). */
  async schedulePreGame(): Promise<number | null> {
    const tip = await this.firstTipOff();
    if (tip === null) {
      await this.ctx.storage.deleteAlarm();
      return null;
    }
    const at = Math.max(tip - PRE_GAME_LEAD_MS, Date.now() + MIN_LEAD_MS);
    if (tip <= Date.now()) {
      await this.ctx.storage.deleteAlarm();
      return null;
    }
    await this.ctx.storage.setAlarm(at);
    return at;
  }

  private async firstTipOff(): Promise<number | null> {
    const fromEspn = await this.tipOffFromEspn();
    if (fromEspn !== null) return fromEspn;
    return this.tipOffFromNbaCdn();
  }

  private async tipOffFromEspn(): Promise<number | null> {
    const dates = etDateKey().replaceAll("-", "");
    try {
      const res = await fetch(`${ESPN_SCOREBOARD}?dates=${dates}`, {
        headers: { Accept: "application/json" },
      });
      if (!res.ok) return null;
      const data = (await res.json()) as {
        events?: { date?: string; status?: { type?: { name?: string } } }[];
      };
      const times = (data.events ?? [])
        .filter((e) => e.status?.type?.name === "STATUS_SCHEDULED" && e.date)
        .map((e) => Date.parse(e.date!))
        .filter((t) => Number.isFinite(t) && t > Date.now());
      return times.length ? Math.min(...times) : null;
    } catch {
      return null;
    }
  }

  private async tipOffFromNbaCdn(): Promise<number | null> {
    try {
      const res = await fetch(NBA_SCOREBOARD, {
        headers: { "User-Agent": "Mozilla/5.0", Accept: "application/json" },
      });
      if (!res.ok) return null;
      const data = (await res.json()) as {
        scoreboard?: { games?: { gameTimeUTC?: string; gameStatus?: number }[] };
      };
      const times = (data.scoreboard?.games ?? [])
        .filter((g) => g.gameStatus === 1 && g.gameTimeUTC)
        .map((g) => Date.parse(g.gameTimeUTC!))
        .filter((t) => Number.isFinite(t) && t > Date.now());
      return times.length ? Math.min(...times) : null;
    } catch {
      return null;
    }
  }

  private async ensureContainer(): Promise<void> {
    const container = this.ctx.container;
    if (!container) throw new Error("container indisponivel neste Durable Object");
    if (!container.running) {
      container.start({
        image: container.images.base,
        instance: "standard-1",
        enableInternet: true,
        env: childEnv(this.env),
      });
    }
    this.starting ??= this.waitForHealth().finally(() => {
      this.starting = undefined;
    });
    await this.starting;
  }

  private async waitForHealth(): Promise<void> {
    const port = this.ctx.container!.getTcpPort(CONTAINER_PORT);
    let lastError: unknown;
    for (let attempt = 0; attempt < 150; attempt++) {
      try {
        const res = await port.fetch("http://container/health", {
          signal: AbortSignal.timeout(2000),
        });
        await res.body?.cancel();
        if (res.ok) return;
        throw new Error(`health ${res.status}`);
      } catch (error) {
        lastError = error;
        await scheduler.wait(300);
      }
    }
    throw new Error("container nao ficou pronto", { cause: lastError });
  }
}

export default {
  async scheduled(controller: ScheduledController, env: Env): Promise<void> {
    await env.RUNNER.getByName("nba-fantasy").fetch(
      new Request("https://do/run?mode=morning"),
    );
  },

  async fetch(request: Request, env: Env): Promise<Response> {
    const url = new URL(request.url);
    if (url.pathname === "/") {
      return new Response(
        "NBA Fantasy Agent. POST /run?token=... para executar agora.",
        { headers: { "Content-Type": "text/plain; charset=utf-8" } },
      );
    }
    if (url.pathname !== "/run") return new Response("not found", { status: 404 });
    if (!env.RUN_TOKEN || url.searchParams.get("token") !== env.RUN_TOKEN) {
      return new Response("unauthorized", { status: 401 });
    }
    const mode = (url.searchParams.get("mode") ?? "manual") as Mode;
    return env.RUNNER.getByName("nba-fantasy").fetch(
      new Request(`https://do/run?mode=${mode}&notify=${url.searchParams.get("notify") ?? "1"}`),
    );
  },
} satisfies ExportedHandler<Env>;
