/* demo_data.js — données de démonstration pour captures d'écran marketing.
   INERTE en production : ne s'active que si l'URL contient ?demo=
   (jamais le cas dans l'app pywebview, qui charge le fichier sans query). */
(function () {
  'use strict';
  const m = /[?&]demo=([a-z-]+)/.exec(window.location.search);
  if (!m) return;
  const vue = m[1];

  const _now = new Date();
  function _iso(heure, minute, jOffset) {
    const d = new Date(_now);
    d.setDate(d.getDate() - (jOffset || 0));
    d.setHours(heure, minute, 0, 0);
    return d.toISOString();
  }

  const CONSULTS = [
    { id: 'd1', date: _iso(11, 30, 0), duration_min: 14, cr_valide: true,
      patient: { nom: 'MARTIN', prenom: 'Claire', motif: 'Angine — douleurs à la déglutition' } },
    { id: 'd2', date: _iso(10, 15, 0), duration_min: 22, cr_valide: true,
      patient: { nom: 'BENALI', prenom: 'Karim', motif: 'Renouvellement traitement hypertension' } },
    { id: 'd3', date: _iso(9, 0, 0),   duration_min: 11, cr_valide: true,
      patient: { nom: 'ROUSSEAU', prenom: 'Emma', motif: 'Certificat de sport' } },
    { id: 'd4', date: _iso(16, 45, 1), duration_min: 18, cr_valide: true,
      patient: { nom: 'LEFEBVRE', prenom: 'Jean', motif: 'Lombalgie persistante' } },
    { id: 'd5', date: _iso(14, 20, 1), duration_min: 16, cr_valide: true,
      patient: { nom: 'NGUYEN', prenom: 'Sophie', motif: 'Suivi diabète type 2' } },
  ];

  const ELEMENTS = {
    motif:        ['Mal de gorge depuis trois jours', 'Douleurs à la déglutition'],
    observations: ['Gorge rouge à l\'examen', 'Fièvre mesurée à 38,2 °C', 'Ganglions cervicaux palpables'],
    traitements:  ['Doliprane 1 g, 3 fois par jour pendant 5 jours', 'Repos recommandé'],
    suivi:        ['Revenir en consultation si pas d\'amélioration sous une semaine'],
  };

  window.api = async function (method) {
    switch (method) {
      case 'get_app_state': return {
        licence_ok: true, licence_expired: false, en_essai: false,
        onboarding_done: true, doctor_name: 'Dr Sophie Marchand',
        specialty: 'Médecine générale', theme: 'light', version: '1.9.0',
        devices_configured: true, mode_consultation: 'cabinet',
      };
      case 'get_stats': return { mois: 42, semaine: 12, patients: 87, duree_moy: 14 };
      case 'get_consultations': return CONSULTS;
      case 'get_cr_a_valider': return { count: 0, dernier_id: null };
      case 'get_update_info': return null;
      case 'file_exists': return { exists: true };
      case 'get_cr_elements': return {
        ready: true, cr_valide: false, date: _iso(1, 0),
        patient: { nom: 'MARTIN', prenom: 'Claire' },
        elements: ELEMENTS,
      };
      default: return null;
    }
  };

  window.addEventListener('load', async () => {
    const state = await window.api('get_app_state');
    licenceValide = true;
    setDoctorIdentity(state.doctor_name, state.specialty);
    await loadHome();
    if (vue === 'validation') {
      await openValidationScreen('d1');
    } else {
      navigate('home');
    }
    document.title = 'Écho';
  });
})();
