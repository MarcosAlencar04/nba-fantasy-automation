/**
 * Dispara o workflow do NBA Fantasy pela API do GitHub, no horario certo.
 *
 * Por que existe
 * --------------
 * O `schedule` do GitHub Actions e' best-effort: a documentacao avisa que
 * "some queued jobs may be dropped" em horarios de pico. Medido neste projeto:
 * ~6% das execucoes agendadas sobrevivem. Um cron no Cloudflare Workers e'
 * pontual de verdade, entao ele e' quem garante a entrega.
 *
 * O disparo NAO substitui os crons do GitHub — os dois convivem. A deduplicacao
 * fica no banco (`state.py`, tabela `agent_notifications`), chaveada por
 * (tipo, dia, time). Se o GitHub e a Cloudflare dispararem no mesmo minuto, o
 * workflow roda duas vezes mas so a primeira notifica.
 *
 * Cron -> modo
 * ------------
 * Cada cron do `wrangler.toml` tem um `mode` associado aqui e vai explicito em
 * `inputs.mode` no POST. Isso deixa o disparo independente da expressao `MODE`
 * do workflow, que so olha `github.event.schedule` (nulo num dispatch).
 */

const DEFAULTS = {
	repo: "MarcosAlencar04/nba-fantasy-automation",
	workflow: "nba-fantasy.yml",
	ref: "development",
	api: "https://api.github.com",
};

/**
 * Cron (UTC) -> modo do workflow.
 *
 * Manha: tres tentativas espalhadas, porque ate o Cloudflare pode atrasar alguns
 * segundos e o GitHub pode estar com fila. A trava do banco impede email triplo.
 * Pre-jogo: mesmo par de minutos que o workflow usa (`5,35`), para o gate de 30
 * min casar com exatamente um tick por janela de jogo.
 */
const CRON_MODES = {
	"7 13 * * *": "morning",
	"37 13 * * *": "morning",
	"7 14 * * *": "morning",
	"5,35 * * * *": "gate",
};

/** Erro de configuracao: nao adianta repetir, o problema e' o secret. */
class ConfigError extends Error {
	constructor(message) {
		super(message);
		this.name = "ConfigError";
	}
}

/** Erro do GitHub (ou de rede) que vale a pena tentar de novo. */
class TransientError extends Error {
	constructor(message) {
		super(message);
		this.name = "TransientError";
	}
}

const CONFIG = {
	repo: (env) => (env.GH_REPO || DEFAULTS.repo).trim(),
	workflow: (env) => (env.GH_WORKFLOW || DEFAULTS.workflow).trim(),
	ref: (env) => (env.GH_REF || DEFAULTS.ref).trim(),
	token: (env) => (env.GH_TOKEN || "").trim(),
	// Sobrescrevivel para GitHub Enterprise e para apontar a um mock em teste.
	api: (env) => (env.GH_API || DEFAULTS.api).trim().replace(/\/+$/, ""),
};

function modeForCron(cron) {
	const mode = CRON_MODES[cron];
	if (mode) {
		return mode;
	}
	// Cron novo no wrangler.toml sem entrada aqui. `gate` e' o lado seguro:
	// consulta a tabela de jogos e so roda perto do tip-off, em vez de mandar
	// relatorio completo fora de hora.
	console.warn(
		`cron '${cron}' nao mapeado em CRON_MODES; usando 'gate'. ` +
			"Adicione a entrada para escolher o modo.",
	);
	return "gate";
}

/** Traduz o status do GitHub no que a pessoa precisa corrigir. */
function explain(status, detail) {
	if (status === 401) {
		return (
			"token invalido ou expirado (401). Gere outro PAT com " +
			"Actions: Read and write e rode `wrangler secret put GH_TOKEN`."
		);
	}
	if (status === 403) {
		return `sem permissao (403). O token precisa de Actions: Read and write. ${detail}`;
	}
	if (status === 404) {
		return (
			"repositorio ou workflow nao encontrado (404). Confira GH_REPO e " +
			`GH_WORKFLOW, e se o token enxerga este repositorio. ${detail}`
		);
	}
	if (status === 422) {
		return (
			`workflow recusou o payload (422): ${detail}. ` +
			"Normalmente e o GH_REF — o arquivo precisa existir naquele branch."
		);
	}
	return `GitHub respondeu ${status}: ${detail}`;
}

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

/**
 * Faz o POST de `workflow_dispatch`.
 *
 * Um retry curto em erro de rede/5xx cobre instabilidade passageira da API sem
 * gastar o tempo da invocacao. 401/403/404/422 nao sao repetidos: sao erros de
 * configuracao e tentar de novo so gera log igual.
 */
async function dispatch(env, mode, { retries = 1, backoffMs = 1500 } = {}) {
	const token = CONFIG.token(env);
	if (!token) {
		throw new ConfigError(
			"GH_TOKEN nao configurado. Rode: wrangler secret put GH_TOKEN",
		);
	}

	const repo = CONFIG.repo(env);
	const workflow = CONFIG.workflow(env);
	const ref = CONFIG.ref(env);
	const url = `${CONFIG.api(env)}/repos/${repo}/actions/workflows/${workflow}/dispatches`;

	let lastError = null;
	for (let attempt = 0; attempt <= retries; attempt += 1) {
		if (attempt > 0) {
			await sleep(backoffMs);
		}

		let response;
		try {
			response = await fetch(url, {
				method: "POST",
				headers: {
					Authorization: `Bearer ${token}`,
					Accept: "application/vnd.github+json",
					"Content-Type": "application/json",
					"User-Agent": "nba-fantasy-cf-worker",
					"X-GitHub-Api-Version": "2022-11-28",
				},
				body: JSON.stringify({ ref, inputs: { mode } }),
			});
		} catch (error) {
			lastError = new TransientError(`erro de rede: ${error.message}`);
			continue;
		}

		// 204 No Content e' o sucesso documentado do endpoint. 200 aparece em
		// respostas com corpo (repositorios novos, proxies).
		if (response.status === 204 || response.status === 200) {
			return { ok: true, mode, repo, workflow, ref, status: response.status };
		}

		const detail = (await response.text().catch(() => "")).slice(0, 300);

		// 5xx e 429 sao passageiros; o resto e' configuracao.
		if (response.status >= 500 || response.status === 429) {
			lastError = new TransientError(explain(response.status, detail));
			continue;
		}

		throw new Error(explain(response.status, detail));
	}

	throw lastError || new Error("falha ao disparar o workflow");
}

/**
 * Autenticacao do gatilho HTTP manual.
 *
 * Sem `TRIGGER_KEY` o endpoint fica fechado (fail closed): um deploy esquecido
 * nao pode virar um botao publico que gasta minutos de Actions.
 */
function keyProblem(env) {
	const key = (env.TRIGGER_KEY || "").trim();
	if (!key) {
		return "TRIGGER_KEY nao configurada (wrangler secret put TRIGGER_KEY).";
	}
	if (key.length < 16) {
		return "TRIGGER_KEY precisa de pelo menos 16 caracteres.";
	}
	return "";
}

async function digest(value) {
	// Compara hashes e nao as strings: `crypto.subtle.timingSafeEqual` nao
	// existe no runtime, e comparar digest de tamanho fixo evita vazar o
	// tamanho da chave pelo tempo de resposta.
	const data = new TextEncoder().encode(value);
	const hash = await crypto.subtle.digest("SHA-256", data);
	return new Uint8Array(hash);
}

async function keyMatches(env, supplied) {
	const expected = (env.TRIGGER_KEY || "").trim();
	if (!expected || !supplied) {
		return false;
	}
	const [a, b] = await Promise.all([digest(expected), digest(supplied)]);
	if (a.length !== b.length) {
		return false;
	}
	let diff = 0;
	for (let i = 0; i < a.length; i += 1) {
		diff |= a[i] ^ b[i];
	}
	return diff === 0;
}

function json(body, status = 200) {
	return new Response(JSON.stringify(body, null, 2), {
		status,
		headers: { "Content-Type": "application/json; charset=utf-8" },
	});
}

export default {
	/**
	 * Chamado pelo Cron Trigger do Cloudflare.
	 *
	 * Erros sobem (`throw`) de proposito: assim a invocacao aparece como falha em
	 * **Cron Events** no dashboard, em vez de "sucesso" com nada feito.
	 */
	async scheduled(controller, env, ctx) {
		const cron = controller.cron;
		const mode = modeForCron(cron);
		console.log(`cron '${cron}' -> mode '${mode}'`);

		const result = await dispatch(env, mode);
		console.log(
			`disparado: ${result.repo}/${result.workflow}@${result.ref} mode=${result.mode} (HTTP ${result.status})`,
		);
	},

	/**
	 * HTTP: `/` responde o estado, `/dispatch` dispara na mao.
	 *
	 * Serve para testar depois do deploy sem esperar o cron, e para um monitor
	 * externo verificar que o worker esta de pe.
	 */
	async fetch(request, env, ctx) {
		const url = new URL(request.url);

		if (url.pathname === "/" || url.pathname === "/health") {
			return json({
				ok: true,
				worker: "nba-fantasy-dispatch",
				repo: CONFIG.repo(env),
				workflow: CONFIG.workflow(env),
				ref: CONFIG.ref(env),
				token: CONFIG.token(env) ? "configurado" : "AUSENTE",
				crons: Object.keys(CRON_MODES),
			});
		}

		if (url.pathname !== "/dispatch") {
			return json({ erro: "rota nao encontrada. Use / ou /dispatch." }, 404);
		}

		const problem = keyProblem(env);
		if (problem) {
			return json({ erro: problem }, 503);
		}

		const supplied = url.searchParams.get("key") ||
			(request.headers.get("x-trigger-key") || "");
		if (!(await keyMatches(env, supplied))) {
			return json({ erro: "chave invalida" }, 401);
		}

		const mode = url.searchParams.get("mode") || "morning";
		if (mode !== "morning" && mode !== "gate") {
			return json({ erro: "mode deve ser 'morning' ou 'gate'." }, 400);
		}

		try {
			const result = await dispatch(env, mode);
			return json(result);
		} catch (error) {
			const status = error instanceof ConfigError ? 503 : 502;
			console.error(`${error.name}: ${error.message}`);
			return json({ erro: error.message }, status);
		}
	},
};

// Exportados para os testes; o runtime so usa o `default`.
export { ConfigError, CRON_MODES, TransientError, dispatch, explain, modeForCron };
