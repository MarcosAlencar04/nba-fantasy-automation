/**
 * Testes do worker de disparo.
 *
 * Roda com `node --test` (Node 24, sem dependencia). O `fetch` global e'
 * substituido, entao nenhum request sai para o GitHub de verdade.
 */
import assert from "node:assert/strict";
import { afterEach, beforeEach, describe, it } from "node:test";

import worker, {
	dispatch,
	explain,
	modeForCron,
} from "../src/index.js";

const ENV = {
	GH_TOKEN: "github_pat_teste",
	GH_REPO: "dono/repo",
	GH_WORKFLOW: "nba-fantasy.yml",
	GH_REF: "development",
	TRIGGER_KEY: "chave-de-teste-com-32-caracteres",
};

const realFetch = globalThis.fetch;
let calls = [];

/** Instala um fetch falso que devolve as respostas na ordem dada. */
function stubFetch(responses) {
	calls = [];
	let index = 0;
	globalThis.fetch = async (url, init) => {
		calls.push({ url, init });
		const spec = responses[Math.min(index, responses.length - 1)];
		index += 1;
		if (spec instanceof Error) {
			throw spec;
		}
		// 204/304 sao "null body status": o construtor do Response recusa
		// qualquer corpo, inclusive string vazia. Sem isto o stub lancaria
		// TypeError e o worker veria como erro de rede.
		const nullBody = spec.status === 204 || spec.status === 304;
		return new Response(nullBody ? null : (spec.body ?? ""), {
			status: spec.status,
		});
	};
}

beforeEach(() => {
	// Silencia os console.* esperados (logs de cron e de erro) para o relatorio
	// do node:test ficar legivel.
	globalThis.console = { ...console, log: () => {}, warn: () => {}, error: () => {} };
});

afterEach(() => {
	globalThis.fetch = realFetch;
});

function scheduled(cron, env = ENV) {
	return worker.scheduled({ cron, scheduledTime: Date.now() }, env, {});
}

function withQuery(path, env = ENV, search = "") {
	return worker.fetch(
		new Request(`https://worker.test${path}${search}`, { method: "GET" }),
		env,
		{},
	);
}

describe("modeForCron", () => {
	it("mapeia os tres crons da manha para morning", () => {
		for (const cron of ["7 13 * * *", "37 13 * * *", "7 14 * * *"]) {
			assert.equal(modeForCron(cron), "morning", cron);
		}
	});

	it("mapeia o cron de pre-jogo para gate", () => {
		assert.equal(modeForCron("5,35 * * * *"), "gate");
	});

	it("cron desconhecido cai em gate (lado seguro)", () => {
		assert.equal(modeForCron("0 0 1 1 *"), "gate");
	});
});

describe("dispatch", () => {
	it("faz POST com headers e corpo corretos", async () => {
		stubFetch([{ status: 204 }]);
		const result = await dispatch(ENV, "morning");

		assert.equal(result.ok, true);
		assert.equal(result.mode, "morning");
		assert.equal(calls.length, 1);

		const [{ url, init }] = calls;
		assert.equal(
			url,
			"https://api.github.com/repos/dono/repo/actions/workflows/nba-fantasy.yml/dispatches",
		);
		assert.equal(init.method, "POST");
		assert.equal(init.headers.Authorization, "Bearer github_pat_teste");
		assert.equal(init.headers.Accept, "application/vnd.github+json");
		assert.equal(init.headers["Content-Type"], "application/json");
		assert.equal(init.headers["X-GitHub-Api-Version"], "2022-11-28");
		assert.deepEqual(JSON.parse(init.body), {
			ref: "development",
			inputs: { mode: "morning" },
		});
	});

	it("passa mode=gate quando pedido", async () => {
		stubFetch([{ status: 204 }]);
		await dispatch(ENV, "gate");
		assert.deepEqual(JSON.parse(calls[0].init.body).inputs, { mode: "gate" });
	});

	it("aceita 200 com corpo", async () => {
		stubFetch([{ status: 200, body: '{"workflow_run_id":1}' }]);
		const result = await dispatch(ENV, "morning");
		assert.equal(result.status, 200);
	});

	it("sem GH_TOKEN falha com instrucao acionavel, sem chamar a API", async () => {
		stubFetch([{ status: 204 }]);
		await assert.rejects(
			() => dispatch({ ...ENV, GH_TOKEN: "" }, "morning"),
			/ConfigError|GH_TOKEN nao configurado/,
		);
		assert.equal(calls.length, 0);
	});

	it("401 explica como gerar o token", async () => {
		stubFetch([{ status: 401, body: '{"message":"Bad credentials"}' }]);
		await assert.rejects(
			() => dispatch(ENV, "morning"),
			/Actions: Read and write/,
		);
	});

	it("404 cita repositorio e workflow", async () => {
		stubFetch([{ status: 404, body: '{"message":"Not Found"}' }]);
		await assert.rejects(() => dispatch(ENV, "morning"), /GH_REPO/);
	});

	it("422 cita o ref", async () => {
		stubFetch([{ status: 422, body: '{"message":"No ref found"}' }]);
		await assert.rejects(() => dispatch(ENV, "morning"), /GH_REF/);
	});

	it("500 e' tentado de novo antes de desistir", async () => {
		stubFetch([{ status: 500, body: "boom" }, { status: 204 }]);
		const result = await dispatch(ENV, "morning", { backoffMs: 0 });
		assert.equal(result.ok, true);
		assert.equal(calls.length, 2);
	});

	it("500 repetido esgota as tentativas", async () => {
		stubFetch([{ status: 503, body: "indisponivel" }]);
		await assert.rejects(
			() => dispatch(ENV, "morning", { retries: 1, backoffMs: 0 }),
			/indisponivel|GitHub respondeu 503/,
		);
		assert.equal(calls.length, 2);
	});

	it("429 tambem e' passageiro", async () => {
		stubFetch([{ status: 429, body: "rate" }, { status: 204 }]);
		const result = await dispatch(ENV, "morning", { backoffMs: 0 });
		assert.equal(result.ok, true);
		assert.equal(calls.length, 2);
	});

	it("erro de rede e' tentado de novo", async () => {
		stubFetch([new Error("ECONNRESET"), { status: 204 }]);
		const result = await dispatch(ENV, "morning", { backoffMs: 0 });
		assert.equal(result.ok, true);
		assert.equal(calls.length, 2);
	});

	it("401 NAO e' repetido (erro de configuracao)", async () => {
		stubFetch([{ status: 401, body: "" }]);
		await assert.rejects(() => dispatch(ENV, "morning", { backoffMs: 0 }));
		assert.equal(calls.length, 1);
	});
});

describe("scheduled", () => {
	it("cron da manha dispara morning", async () => {
		stubFetch([{ status: 204 }]);
		await scheduled("7 13 * * *");
		assert.deepEqual(JSON.parse(calls[0].init.body).inputs, { mode: "morning" });
	});

	it("cron de pre-jogo dispara gate", async () => {
		stubFetch([{ status: 204 }]);
		await scheduled("5,35 * * * *");
		assert.deepEqual(JSON.parse(calls[0].init.body).inputs, { mode: "gate" });
	});

	it("propaga a falha para a invocacao ser marcada como erro", async () => {
		stubFetch([{ status: 401, body: "" }]);
		await assert.rejects(() => scheduled("7 13 * * *"));
	});

	it("o token nunca aparece no corpo enviado", async () => {
		stubFetch([{ status: 204 }]);
		await scheduled("7 13 * * *");
		assert.equal(calls[0].init.body.includes(ENV.GH_TOKEN), false);
	});
});

describe("fetch /health", () => {
	it("responde o estado sem expor o token", async () => {
		const response = await withQuery("/health");
		const body = await response.json();

		assert.equal(response.status, 200);
		assert.equal(body.ok, true);
		assert.equal(body.token, "configurado");
		assert.equal(body.repo, "dono/repo");
		assert.equal(JSON.stringify(body).includes(ENV.GH_TOKEN), false);
	});

	it("denuncia token ausente", async () => {
		const response = await withQuery("/", { ...ENV, GH_TOKEN: "" });
		assert.equal((await response.json()).token, "AUSENTE");
	});

	it("rota desconhecida e' 404", async () => {
		assert.equal((await withQuery("/nada")).status, 404);
	});
});

describe("fetch /dispatch", () => {
	it("dispara com a chave certa", async () => {
		stubFetch([{ status: 204 }]);
		const response = await withQuery(
			"/dispatch",
			ENV,
			`?mode=gate&key=${ENV.TRIGGER_KEY}`,
		);
		assert.equal(response.status, 200);
		assert.equal((await response.json()).mode, "gate");
	});

	it("aceita a chave pelo header", async () => {
		stubFetch([{ status: 204 }]);
		const response = await worker.fetch(
			new Request("https://worker.test/dispatch?mode=morning", {
				method: "GET",
				headers: { "x-trigger-key": ENV.TRIGGER_KEY },
			}),
			ENV,
			{},
		);
		assert.equal(response.status, 200);
	});

	it("chave errada e' 401 e nao chama a API", async () => {
		stubFetch([{ status: 204 }]);
		const response = await withQuery("/dispatch?key=errada", ENV);
		assert.equal(response.status, 401);
		assert.equal(calls.length, 0);
	});

	it("prefixo da chave nao serve", async () => {
		stubFetch([{ status: 204 }]);
		const short = ENV.TRIGGER_KEY.slice(0, -1);
		assert.equal((await withQuery(`/dispatch?key=${short}`, ENV)).status, 401);
	});

	it("sem TRIGGER_KEY o endpoint fica fechado (fail closed)", async () => {
		stubFetch([{ status: 204 }]);
		const response = await withQuery(
			"/dispatch?key=qualquer",
			{ ...ENV, TRIGGER_KEY: "" },
		);
		assert.equal(response.status, 503);
		assert.equal(calls.length, 0);
	});

	it("chave curta mantem o endpoint fechado", async () => {
		stubFetch([{ status: 204 }]);
		const response = await withQuery(
			"/dispatch?key=curta",
			{ ...ENV, TRIGGER_KEY: "curta" },
		);
		assert.equal(response.status, 503);
	});

	it("mode invalido e' 400", async () => {
		stubFetch([{ status: 204 }]);
		const response = await withQuery(
			`/dispatch?mode=manha&key=${ENV.TRIGGER_KEY}`,
			ENV,
		);
		assert.equal(response.status, 400);
		assert.equal(calls.length, 0);
	});

	it("erro de configuracao vira 503", async () => {
		stubFetch([{ status: 204 }]);
		const response = await withQuery(
			`/dispatch?key=${ENV.TRIGGER_KEY}`,
			{ ...ENV, GH_TOKEN: "" },
		);
		assert.equal(response.status, 503);
	});

	it("erro do GitHub vira 502 e nao vaza o token", async () => {
		stubFetch([{ status: 401, body: '{"message":"Bad credentials"}' }]);
		const response = await withQuery(
			`/dispatch?key=${ENV.TRIGGER_KEY}`,
			ENV,
		);
		const text = await response.text();
		assert.equal(response.status, 502);
		assert.equal(text.includes(ENV.GH_TOKEN), false);
	});
});

describe("explain", () => {
	it("cobre os status documentados", () => {
		assert.match(explain(401, ""), /Actions: Read and write/);
		assert.match(explain(403, ""), /permissao/);
		assert.match(explain(404, ""), /nao encontrado/);
		assert.match(explain(422, ""), /GH_REF/);
		assert.match(explain(500, "x"), /500/);
	});
});
