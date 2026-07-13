import { serve } from "https://deno.land/std@0.168.0/http/server.ts"
import { createClient } from "https://esm.sh/@supabase/supabase-js@2"

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
  if (path === "/inscription" && req.method === "POST") {
    const { email, mot_de_passe, nom, specialite } = await req.json()

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

    const encoder = new TextEncoder()
    const data = encoder.encode(mot_de_passe + email)
    const hashBuffer = await crypto.subtle.digest("SHA-256", data)
    const hashArray = Array.from(new Uint8Array(hashBuffer))
    const hash = hashArray.map(b => b.toString(16).padStart(2, "0")).join("")

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

  return new Response(
    JSON.stringify({ ok: false, erreur: "Route introuvable" }),
    { status: 404, headers: corsHeaders }
  )
})