/* Pile de navigation (historique « ← Retour »).
   Logique pure, sans DOM — testée sous Node par tests/test_navigation.py.
   Chaque entrée : { view, key, restore, label }
     view    : nom de la vue (data-view)
     key     : identité du contexte (id consultation, nom patient…) —
               deux entrées identiques (view+key) ne sont jamais empilées
               l'une sur l'autre (pas de doublons)
     restore : fonction async qui ré-affiche la page AVEC son contexte
     label   : libellé optionnel du bouton retour qui y mène */
const NavStack = (() => {
  let stack = [];

  function _memeEntree(a, b) {
    return a && b && a.view === b.view && (a.key || '') === (b.key || '');
  }

  return {
    /* Empile une entrée, sauf si c'est un doublon du sommet. */
    push(entry) {
      if (!entry || !entry.view) return;
      if (_memeEntree(stack[stack.length - 1], entry)) return;
      stack.push(entry);
    },
    /* Dépile et renvoie l'entrée précédente, ou null (pile vide →
       l'appelant retombe sur l'accueil). */
    pop()  { return stack.pop() || null; },
    peek() { return stack[stack.length - 1] || null; },
    /* Vidée au retour à l'accueil et à la déconnexion. */
    clear() { stack = []; },
    size()  { return stack.length; },
  };
})();

/* Export Node pour les tests (inerte dans le navigateur). */
if (typeof module !== 'undefined' && module.exports) module.exports = NavStack;
