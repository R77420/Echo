// Test du mode bêta de l'Edge Function echo-api — exécute le VRAI index.ts.
//
// Deno n'est pas requis : on charge supabase/functions/echo-api/index.ts,
// on retire ses deux imports distants, on efface les types (Node >= 22.13)
// et on injecte `serve`, `createClient` et `Deno.env` factices. La base est
// simulée : une licence dont l'essai du médecin est EXPIRÉ depuis 30 jours.
//
// Lancer : node tests/edge/test_mode_beta.mjs   (ou via pytest test_edge_beta.py)
import assert from "node:assert/strict"
import { readFileSync } from "node:fs"
import { stripTypeScriptTypes } from "node:module"
import { dirname, join } from "node:path"
import { fileURLToPath } from "node:url"

const racine = join(dirname(fileURLToPath(import.meta.url)), "..", "..")
const source = readFileSync(
  join(racine, "supabase", "functions", "echo-api", "index.ts"), "utf8")

const sansImports = source
  .split("\n").filter(l => !/^import .* from "https:\/\//.test(l)).join("\n")
assert.ok(!/^import /m.test(sansImports), "imports inattendus dans index.ts")
const js = stripTypeScriptTypes(sansImports)

const JOUR = 24 * 3600 * 1000
const MEDECIN_EXPIRE = {
  id: "m-1", email: "expire@test.fr", nom: "Dr Test", licence_active: false,
  essai_fin: new Date(Date.now() - 30 * JOUR).toISOString(),
  mot_de_passe_hash: null,     // renseigné plus bas (SHA-256(mdp + email))
}
const LICENCE = { cle_licence: "CLE-EXPIREE", medecin_id: "m-1", active: false }

// Faux client Supabase : requêtes chaînables, filtrées par .eq().
function fauxClient() {
  return {
    from(table) {
      const filtres = {}
      const lignes = () => {
        let base = []
        if (table === "licences") base = [{ ...LICENCE, medecins: MEDECIN_EXPIRE }]
        if (table === "medecins") base = [MEDECIN_EXPIRE]
        return base.filter(l => Object.entries(filtres).every(([k, v]) => l[k] === v))
      }
      const q = {
        select() { return q },
        eq(k, v) { filtres[k] = v; return q },
        limit() { return q },
        single() { const l = lignes(); return Promise.resolve({ data: l[0] ?? null }) },
        then(res) { return Promise.resolve({ data: lignes() }).then(res) },
      }
      return q
    },
  }
}

function charger(env, logs) {
  let handler = null
  const Deno = { env: { get: k => env[k] } }
  const fabrique = new Function("serve", "createClient", "Deno", "console", js)
  fabrique(h => { handler = h }, () => fauxClient(), Deno,
           { log: m => logs.push(String(m)), error: m => logs.push(String(m)) })
  assert.ok(handler, "serve() n'a pas reçu de handler")
  return async (chemin, corps) => {
    const rep = await handler(new Request("http://x/echo-api" + chemin, {
      method: "POST", body: JSON.stringify(corps),
      headers: { "Content-Type": "application/json" },
    }))
    return { status: rep.status, json: await rep.json() }
  }
}

async function sha256(texte) {
  const buf = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(texte))
  return Array.from(new Uint8Array(buf)).map(b => b.toString(16).padStart(2, "0")).join("")
}
MEDECIN_EXPIRE.mot_de_passe_hash = await sha256("secret" + MEDECIN_EXPIRE.email)

const ENV = { SUPABASE_URL: "http://x", SUPABASE_SERVICE_ROLE_KEY: "k",
              STRIPE_SECRET_KEY: "s", PRICE_INSTALLATION: "p1",
              PRICE_ABONNEMENT: "p2", ADMIN_KEY: "a" }

// ── 1. Sans le secret : comportement actuel inchangé (essai expiré → refusé).
for (const valeur of [undefined, "0", "true", ""]) {
  const logs = []
  const appel = charger({ ...ENV, ECHO_MODE_BETA: valeur }, logs)
  const r = await appel("/verifier-licence", { cle_licence: "CLE-EXPIREE" })
  assert.equal(r.json.ok, true)
  assert.equal(r.json.valide, false, `ECHO_MODE_BETA=${valeur} ne doit rien changer`)
  assert.equal(r.json.beta, undefined)
  assert.equal(r.json.jours_restants, 0)
  assert.ok(!logs.some(l => l.includes("mode bêta actif")))
  const c = await appel("/connexion", { email: MEDECIN_EXPIRE.email, mot_de_passe: "secret" })
  assert.equal(c.json.ok, true)
  assert.equal(c.json.beta, undefined)
}

// ── 2. ECHO_MODE_BETA=1 : compte à l'essai expiré → valide, beta:true, loggé.
{
  const logs = []
  const appel = charger({ ...ENV, ECHO_MODE_BETA: "1" }, logs)
  const r = await appel("/verifier-licence", { cle_licence: "CLE-EXPIREE" })
  assert.deepEqual(
    { ok: r.json.ok, valide: r.json.valide, beta: r.json.beta,
      en_essai: r.json.en_essai, jours_restants: r.json.jours_restants },
    { ok: true, valide: true, beta: true, en_essai: false, jours_restants: 0 })
  assert.equal(logs.filter(l => l.includes("mode bêta actif")).length, 1,
               "« mode bêta actif » doit être loggé à chaque vérification")
  await appel("/verifier-licence", { cle_licence: "CLE-EXPIREE" })
  assert.equal(logs.filter(l => l.includes("mode bêta actif")).length, 2)

  // Clé inconnue : toujours refusée (bêta = comptes EXISTANTS uniquement).
  const inconnu = await appel("/verifier-licence", { cle_licence: "N-EXISTE-PAS" })
  assert.deepEqual(inconnu.json, { ok: false, valide: false })

  // Connexion : jamais refusée pour motif de licence, mauvais mot de passe si.
  const c = await appel("/connexion", { email: MEDECIN_EXPIRE.email, mot_de_passe: "secret" })
  assert.equal(c.json.ok, true)
  assert.equal(c.json.valide, true)
  assert.equal(c.json.beta, true)
  assert.equal(c.json.en_essai, false)
  assert.equal(c.json.cle_licence, "CLE-EXPIREE")
  const ko = await appel("/connexion", { email: MEDECIN_EXPIRE.email, mot_de_passe: "faux" })
  assert.equal(ko.json.ok, false)
}

console.log("OK — mode bêta : essai expiré → valide:true, beta:true ; hors bêta inchangé")
