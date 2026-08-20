import { serve } from "https://deno.land/std@0.168.0/http/server.ts"
import { createClient } from "https://esm.sh/@supabase/supabase-js@2"

// Hash mot de passe — MÊME schéma partout (inscription, connexion, reset) :
// SHA-256(mot_de_passe + email), hex.
async function hashMotDePasse(motDePasse: string, email: string): Promise<string> {
  const data = new TextEncoder().encode(motDePasse + email)
  const buf = await crypto.subtle.digest("SHA-256", data)
  return Array.from(new Uint8Array(buf)).map(b => b.toString(16).padStart(2, "0")).join("")
}

// Code à 6 chiffres cryptographiquement aléatoire (jamais Math.random).
function genererCode(): string {
  const n = new Uint32Array(1)
  crypto.getRandomValues(n)
  return String(n[0] % 1_000_000).padStart(6, "0")
}

// Jeton opaque prouvant l'email vérifié (stocké en base, courte durée).
function genererJeton(): string {
  const b = new Uint8Array(32)
  crypto.getRandomValues(b)
  return Array.from(b).map(x => x.toString(16).padStart(2, "0")).join("")
}

const corsHeaders = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Headers": "Content-Type, Authorization, X-Admin-Key",
  "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
  "Content-Type": "application/json",
}

serve(async (req) => {
  if (req.method === "OPTIONS") {
    return new Response(null, { headers: corsHeaders })
  }

  const url = new URL(req.url)
  const path = url.pathname.replace("/echo-api", "")

  const supabaseUrl = Deno.env.get("SUPABASE_URL")!
  const supabaseKey = Deno.env.get("SUPABASE_SERVICE_ROLE_KEY")!
  const stripeKey = Deno.env.get("STRIPE_SECRET_KEY")!
  const priceInstallation = Deno.env.get("PRICE_INSTALLATION")!
  const priceAbonnement = Deno.env.get("PRICE_ABONNEMENT")!
  const adminKey = Deno.env.get("ADMIN_KEY")!

  const supabase = createClient(supabaseUrl, supabaseKey)

  // ═══ INSCRIPTION ═══
  // Exige un jeton_verification obtenu via /demander-code + /verifier-code :
  // l'email est PROUVÉ avant toute création de compte.
  if (path === "/inscription" && req.method === "POST") {
    const { email, mot_de_passe, nom, specialite, jeton_verification } = await req.json()

    if (!jeton_verification) {
      return new Response(
        JSON.stringify({ ok: false, verification_requise: true,
                         erreur: "Vérification de l'email requise" }),
        { status: 401, headers: corsHeaders }
      )
    }
    const { data: jetons } = await supabase
      .from("jetons_verification")
      .select("*")
      .eq("email", email).eq("type", "inscription").eq("jeton", jeton_verification)
      .limit(1)
    const jeton = jetons && jetons[0]
    if (!jeton || new Date(jeton.expire_le).getTime() < Date.now()) {
      return new Response(
        JSON.stringify({ ok: false, verification_requise: true,
                         erreur: "Vérification expirée, recommencez" }),
        { status: 401, headers: corsHeaders }
      )
    }

    const { data: existant } = await supabase
      .from("medecins")
      .select("id")
      .eq("email", email)

    if (existant && existant.length > 0) {
      return new Response(
        JSON.stringify({ ok: false, erreur: "Email déjà utilisé" }),
        { headers: corsHeaders }
      )
    }

    const hash = await hashMotDePasse(mot_de_passe, email)

    const { data: medecin, error } = await supabase
      .from("medecins")
      .insert({
        email, mot_de_passe_hash: hash, nom,
        specialite: specialite || "Médecin généraliste",
        licence_active: false,
      })
      .select()
      .single()

    if (error || !medecin) {
      return new Response(
        JSON.stringify({ ok: false, erreur: "Erreur création compte" }),
        { headers: corsHeaders }
      )
    }

    // Créer client Stripe
    const stripeRes = await fetch("https://api.stripe.com/v1/customers", {
      method: "POST",
      headers: {
        "Authorization": `Bearer ${stripeKey}`,
        "Content-Type": "application/x-www-form-urlencoded",
      },
      body: `email=${encodeURIComponent(email)}&name=${encodeURIComponent(nom)}`,
    })
    const stripeCustomer = await stripeRes.json()

    await supabase
      .from("medecins")
      .update({ stripe_customer_id: stripeCustomer.id })
      .eq("id", medecin.id)

    const { data: licence } = await supabase
      .from("licences")
      .insert({ medecin_id: medecin.id, active: false })
      .select()
      .single()

    // Jeton à usage unique : consommé par la création du compte.
    await supabase.from("jetons_verification").delete().eq("id", jeton.id)

    return new Response(JSON.stringify({
      ok: true,
      medecin_id: medecin.id,
      essai_fin: medecin.essai_fin,
      cle_licence: licence?.cle_licence,
    }), { headers: corsHeaders })
  }

  // ═══ CONNEXION ═══
  if (path === "/connexion" && req.method === "POST") {
    const { email, mot_de_passe } = await req.json()

    const encoder = new TextEncoder()
    const data = encoder.encode(mot_de_passe + email)
    const hashBuffer = await crypto.subtle.digest("SHA-256", data)
    const hashArray = Array.from(new Uint8Array(hashBuffer))
    const hash = hashArray.map(b => b.toString(16).padStart(2, "0")).join("")

    const { data: medecin } = await supabase
      .from("medecins")
      .select("*")
      .eq("email", email)
      .eq("mot_de_passe_hash", hash)
      .single()

    if (!medecin) {
      return new Response(
        JSON.stringify({ ok: false, erreur: "Email ou mot de passe incorrect" }),
        { headers: corsHeaders }
      )
    }

    const maintenant = new Date()
    const essaiFin = new Date(medecin.essai_fin)
    const joursRestants = Math.ceil((essaiFin.getTime() - maintenant.getTime()) / (1000 * 60 * 60 * 24))

    const { data: licence } = await supabase
      .from("licences")
      .select("*")
      .eq("medecin_id", medecin.id)
      .single()

    return new Response(JSON.stringify({
      ok: true,
      medecin_id: medecin.id,
      nom: medecin.nom,
      email: medecin.email,
      licence_active: medecin.licence_active,
      en_essai: !medecin.licence_active && joursRestants > 0,
      jours_restants: joursRestants,
      cle_licence: licence?.cle_licence,
    }), { headers: corsHeaders })
  }

  // ═══ VÉRIFICATION LICENCE ═══
  if (path === "/verifier-licence" && req.method === "POST") {
    const { cle_licence } = await req.json()

    const { data: licence } = await supabase
      .from("licences")
      .select("*, medecins(*)")
      .eq("cle_licence", cle_licence)
      .single()

    if (!licence) {
      return new Response(
        JSON.stringify({ ok: false, valide: false }),
        { headers: corsHeaders }
      )
    }

    const medecin = licence.medecins
    const maintenant = new Date()
    const essaiFin = new Date(medecin.essai_fin)
    const joursRestants = Math.ceil((essaiFin.getTime() - maintenant.getTime()) / (1000 * 60 * 60 * 24))
    const valide = medecin.licence_active || joursRestants > 0

    return new Response(JSON.stringify({
      ok: true,
      valide,
      licence_active: medecin.licence_active,
      en_essai: !medecin.licence_active && joursRestants > 0,
      jours_restants: Math.max(0, joursRestants),
      nom: medecin.nom,
    }), { headers: corsHeaders })
  }

  // ═══ CRÉER LIEN DE PAIEMENT ═══
  if (path === "/creer-paiement" && req.method === "POST") {
    const { medecin_id, type } = await req.json()

    const { data: medecin } = await supabase
      .from("medecins")
      .select("stripe_customer_id, email, nom")
      .eq("id", medecin_id)
      .single()

    if (!medecin) {
      return new Response(
        JSON.stringify({ ok: false, erreur: "Médecin introuvable" }),
        { headers: corsHeaders }
      )
    }

    const priceId = type === "installation" ? priceInstallation : priceAbonnement

    const sessionRes = await fetch("https://api.stripe.com/v1/checkout/sessions", {
      method: "POST",
      headers: {
        "Authorization": `Bearer ${stripeKey}`,
        "Content-Type": "application/x-www-form-urlencoded",
      },
      body: [
        `customer=${medecin.stripe_customer_id}`,
        `mode=${type === "installation" ? "payment" : "subscription"}`,
        `line_items[0][price]=${priceId}`,
        `line_items[0][quantity]=1`,
        `success_url=https://echo-fr-transcrption.lovable.app/merci`,
        `cancel_url=https://echo-fr-transcrption.lovable.app/paiement`,
        `metadata[medecin_id]=${medecin_id}`,
        `metadata[type]=${type}`,
      ].join("&"),
    })
    const session = await sessionRes.json()

    return new Response(
      JSON.stringify({ ok: true, url: session.url }),
      { headers: corsHeaders }
    )
  }

  // ═══ ADMIN ═══
  if (path === "/admin/medecins" && req.method === "GET") {
    const adminKeyHeader = req.headers.get("X-Admin-Key")
    if (adminKeyHeader !== adminKey) {
      return new Response(
        JSON.stringify({ ok: false, erreur: "Non autorisé" }),
        { status: 401, headers: corsHeaders }
      )
    }

    const { data: medecins } = await supabase
      .from("medecins")
      .select("id,email,nom,specialite,licence_active,essai_debut,essai_fin,cree_le")

    const { data: paiements } = await supabase
      .from("paiements")
      .select("*")
      .order("cree_le", { ascending: false })
      .limit(50)

    return new Response(
      JSON.stringify({ ok: true, medecins, paiements }),
      { headers: corsHeaders }
    )
  }

  // ═══ MISE À JOUR SPÉCIALITÉ ═══
  if (path === "/maj-specialite" && req.method === "POST") {
    const { medecin_id, specialite } = await req.json()

    if (!medecin_id) {
      return new Response(
        JSON.stringify({ ok: false, erreur: "medecin_id manquant" }),
        { status: 400, headers: corsHeaders }
      )
    }

    const { error } = await supabase
      .from("medecins")
      .update({ specialite: specialite || "Médecin généraliste" })
      .eq("id", medecin_id)

    if (error) {
      return new Response(
        JSON.stringify({ ok: false, erreur: "Erreur mise à jour spécialité" }),
        { status: 500, headers: corsHeaders }
      )
    }

    return new Response(
      JSON.stringify({ ok: true }),
      { headers: corsHeaders }
    )
  }

  // ═══ DEMANDER UN CODE DE VÉRIFICATION (inscription / reset) ═══
  if (path === "/demander-code" && req.method === "POST") {
    const { email, type } = await req.json()

    if (!email || !["inscription", "reset"].includes(type)) {
      return new Response(
        JSON.stringify({ ok: false, erreur: "email ou type manquant" }),
        { status: 400, headers: corsHeaders }
      )
    }

    const { data: existants } = await supabase
      .from("medecins")
      .select("id")
      .eq("email", email)
    const compteExiste = !!(existants && existants.length > 0)

    if (type === "inscription" && compteExiste) {
      return new Response(
        JSON.stringify({ ok: false, email_pris: true,
                         erreur: "Un compte existe déjà avec cet email" }),
        { headers: corsHeaders }
      )
    }
    if (type === "reset" && !compteExiste) {
      // Réponse NEUTRE : ne jamais révéler si un email est enregistré.
      // Aucun code n'est créé ni envoyé.
      console.log(`demander-code reset: email inconnu (réponse neutre)`)
      return new Response(JSON.stringify({ ok: true }), { headers: corsHeaders })
    }

    // Anti-abus : max 3 codes par (email, type) sur 15 minutes.
    const il15min = new Date(Date.now() - 15 * 60 * 1000).toISOString()
    const { count: recents } = await supabase
      .from("codes_verification")
      .select("id", { count: "exact", head: true })
      .eq("email", email).eq("type", type)
      .gte("cree_le", il15min)
    if ((recents ?? 0) >= 3) {
      return new Response(
        JSON.stringify({ ok: false,
                         erreur: "Trop de demandes, réessayez dans quelques minutes" }),
        { status: 429, headers: corsHeaders }
      )
    }

    const resendKey = Deno.env.get("RESEND_API_KEY")
    if (!resendKey) {
      console.error("demander-code: RESEND_API_KEY absente des secrets")
      return new Response(
        JSON.stringify({ ok: false,
                         erreur: "Impossible d'envoyer l'email pour le moment, réessayez dans quelques minutes" }),
        { status: 500, headers: corsHeaders }
      )
    }

    const code = genererCode()

    // Envoi AVANT insertion : jamais de code en base sans email parti.
    // From : sous-domaine send.echo-medical.fr — le seul vérifié dans Resend.
    const resendRes = await fetch("https://api.resend.com/emails", {
      method: "POST",
      headers: {
        "Authorization": `Bearer ${resendKey}`,
        "Content-Type": "application/json",
      },
      body: JSON.stringify({
        from: "Écho <verification@send.echo-medical.fr>",
        to: [email],
        subject: "Votre code de vérification Écho",
        html:
          `<div style="font-family:Arial,Helvetica,sans-serif;max-width:480px;margin:0 auto;color:#14302A;">` +
          `<p>Bonjour,</p>` +
          `<p>Votre code de vérification Écho est : <strong style="font-size:20px;letter-spacing:2px;">${code}</strong></p>` +
          `<p>Il expire dans 10 minutes.</p>` +
          `<p>Si vous n'êtes pas à l'origine de cette demande, ignorez cet email.</p>` +
          `<hr style="border:none;border-top:1px solid #E9ECE9;margin:18px 0;">` +
          `<p style="font-size:12px;color:#6B7C76;">Écho — assistant de consultation médicale<br>` +
          `Cet email a été envoyé automatiquement, merci de ne pas y répondre.</p>` +
          `</div>`,
      }),
    })
    if (!resendRes.ok) {
      console.error(`demander-code: Resend HTTP ${resendRes.status} — ${await resendRes.text()}`)
      return new Response(
        JSON.stringify({ ok: false,
                         erreur: "Impossible d'envoyer l'email pour le moment, réessayez dans quelques minutes" }),
        { status: 502, headers: corsHeaders }
      )
    }

    // Purge des anciens codes du même couple, puis insertion du nouveau.
    await supabase.from("codes_verification")
      .delete().eq("email", email).eq("type", type)
    const { error: errInsert } = await supabase.from("codes_verification").insert({
      email, code, type,
      expire_le: new Date(Date.now() + 10 * 60 * 1000).toISOString(),
    })
    if (errInsert) {
      console.error("demander-code: insertion code impossible", errInsert)
      return new Response(
        JSON.stringify({ ok: false, erreur: "Erreur serveur, réessayez" }),
        { status: 500, headers: corsHeaders }
      )
    }

    console.log(`demander-code: code ${type} envoyé à ${email}`)
    return new Response(JSON.stringify({ ok: true }), { headers: corsHeaders })
  }

  // ═══ VÉRIFIER UN CODE → JETON ═══
  if (path === "/verifier-code" && req.method === "POST") {
    const { email, code, type } = await req.json()

    if (!email || !code || !["inscription", "reset"].includes(type)) {
      return new Response(
        JSON.stringify({ ok: false, erreur: "email, code ou type manquant" }),
        { status: 400, headers: corsHeaders }
      )
    }

    const { data: enr } = await supabase
      .from("codes_verification")
      .select("*")
      .eq("email", email).eq("type", type)
      .order("cree_le", { ascending: false })
      .limit(1)
    const c = enr && enr[0]

    if (!c) {
      return new Response(
        JSON.stringify({ ok: false, erreur: "Code incorrect" }),
        { headers: corsHeaders }
      )
    }
    if (c.tentatives >= 5) {
      return new Response(
        JSON.stringify({ ok: false,
                         erreur: "Trop de tentatives, demandez un nouveau code" }),
        { headers: corsHeaders }
      )
    }
    if (new Date(c.expire_le).getTime() < Date.now()) {
      return new Response(
        JSON.stringify({ ok: false, erreur: "Code expiré" }),
        { headers: corsHeaders }
      )
    }
    if (c.code !== String(code).trim()) {
      await supabase.from("codes_verification")
        .update({ tentatives: c.tentatives + 1 }).eq("id", c.id)
      return new Response(
        JSON.stringify({ ok: false, erreur: "Code incorrect" }),
        { headers: corsHeaders }
      )
    }

    // Succès : le code est consommé, un jeton court (15 min) le remplace.
    const jeton = genererJeton()
    const { error: errJeton } = await supabase.from("jetons_verification").insert({
      email, type, jeton,
      expire_le: new Date(Date.now() + 15 * 60 * 1000).toISOString(),
    })
    if (errJeton) {
      console.error("verifier-code: insertion jeton impossible", errJeton)
      return new Response(
        JSON.stringify({ ok: false, erreur: "Erreur serveur, réessayez" }),
        { status: 500, headers: corsHeaders }
      )
    }
    await supabase.from("codes_verification").delete().eq("id", c.id)

    console.log(`verifier-code: email ${email} vérifié (${type})`)
    return new Response(
      JSON.stringify({ ok: true, jeton_verification: jeton }),
      { headers: corsHeaders }
    )
  }

  // ═══ RÉINITIALISER LE MOT DE PASSE ═══
  if (path === "/reinitialiser-mot-de-passe" && req.method === "POST") {
    const { email, jeton_verification, nouveau_mot_de_passe } = await req.json()

    if (!email || !jeton_verification || !nouveau_mot_de_passe) {
      return new Response(
        JSON.stringify({ ok: false, erreur: "Champs manquants" }),
        { status: 400, headers: corsHeaders }
      )
    }

    const { data: jetons } = await supabase
      .from("jetons_verification")
      .select("*")
      .eq("email", email).eq("type", "reset").eq("jeton", jeton_verification)
      .limit(1)
    const j = jetons && jetons[0]
    if (!j || new Date(j.expire_le).getTime() < Date.now()) {
      return new Response(
        JSON.stringify({ ok: false, erreur: "Vérification expirée, recommencez" }),
        { status: 401, headers: corsHeaders }
      )
    }

    const hash = await hashMotDePasse(nouveau_mot_de_passe, email)
    const { error: errMaj } = await supabase
      .from("medecins")
      .update({ mot_de_passe_hash: hash })
      .eq("email", email)
    if (errMaj) {
      console.error("reinitialiser-mot-de-passe:", errMaj)
      return new Response(
        JSON.stringify({ ok: false, erreur: "Erreur serveur, réessayez" }),
        { status: 500, headers: corsHeaders }
      )
    }

    // Jeton à usage unique : invalidé après emploi.
    await supabase.from("jetons_verification").delete().eq("id", j.id)

    console.log(`reinitialiser-mot-de-passe: mot de passe changé pour ${email}`)
    return new Response(JSON.stringify({ ok: true }), { headers: corsHeaders })
  }

  return new Response(
    JSON.stringify({ ok: false, erreur: "Route introuvable" }),
    { status: 404, headers: corsHeaders }
  )
})