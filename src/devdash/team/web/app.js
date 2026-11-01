// The team server's page: sign up or in, organisations, projects and the explanations shared
// to them, teams, people and invites, and approving a DevDash that asks to link. Text from the
// server is only ever set as text (never as HTML), and the page loads nothing from elsewhere.
"use strict";

const main = document.getElementById("main");
const ORG_KEY = "devdash-team-org";
let state = null;
// The organisation on show: { id, name, role }.
let org = null;

async function api(path, method = "GET", body) {
  const response = await fetch(path, {
    method,
    credentials: "same-origin",
    headers: body === undefined ? {} : { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  let data = null;
  try {
    data = await response.json();
  } catch {
    data = null;
  }
  if (!response.ok) {
    const detail = data && data.detail;
    const message = typeof detail === "string" ? detail
      : Array.isArray(detail) ? detail.map((d) => `${(d.loc || []).slice(-1)[0]}: ${d.msg}`).join("; ")
      : `Something went wrong (${response.status}).`;
    const error = new Error(message);
    error.status = response.status;
    throw error;
  }
  return data;
}

// The current organisation's part of the API.
const here = (path) => `/api/orgs/${org.id}${path}`;
const isAdmin = () => org && ["owner", "admin"].includes(org.role);

// h("p", { class: "x", onclick: fn }, "text", child) builds an element; strings become text.
function h(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs || {})) {
    if (value === undefined || value === null || value === false) continue;
    if (key.startsWith("on")) node.addEventListener(key.slice(2), value);
    else if (key === "class") node.className = value;
    else node.setAttribute(key, value === true ? "" : value);
  }
  for (const child of children.flat()) {
    if (child === null || child === undefined || child === false) continue;
    node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return node;
}

// Lists are spread and empty parts (null, false) left out.
function show(...nodes) {
  main.replaceChildren(...nodes.flat().filter((node) => node !== null && node !== undefined && node !== false));
}

function when(iso) {
  if (!iso) return "never";
  const date = new Date(iso);
  const days = Math.round((Date.now() - date.getTime()) / 86400000);
  const time = date.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
  if (days === 0) return `today, ${time}`;
  if (days === 1) return `yesterday, ${time}`;
  return date.toLocaleDateString([], { day: "numeric", month: "short", year: days > 300 ? "numeric" : undefined });
}

// A form whose button shows progress and whose error shows under it.
function form(fields, label, submit) {
  const error = h("p", { class: "error", role: "alert" });
  const button = h("button", { class: "primary", type: "submit" }, label);
  const node = h("form", {
    class: "form",
    onsubmit: async (event) => {
      event.preventDefault();
      error.textContent = "";
      button.disabled = true;
      const values = Object.fromEntries(new FormData(node).entries());
      try {
        await submit(values);
      } catch (e) {
        error.textContent = e.message;
      } finally {
        button.disabled = false;
      }
    },
  }, fields, button, error);
  return node;
}

function field(label, name, type = "text", extra = {}) {
  return h("label", { class: "field" }, h("span", {}, label),
    h("input", { name, type, required: true, autocapitalize: "off", spellcheck: "false", ...extra }));
}

function card(...children) {
  return h("section", { class: "card" }, ...children);
}

function action(label, run, kind = "") {
  return h("button", {
    class: `small ${kind}`,
    type: "button",
    onclick: async (event) => {
      const button = event.currentTarget;
      button.disabled = true;
      try {
        await run();
      } catch (e) {
        alert(e.message);
      } finally {
        button.disabled = false;
      }
    },
  }, label);
}

function remember(id) {
  try {
    localStorage.setItem(ORG_KEY, String(id));
  } catch {
    // Private windows may refuse; the first organisation shows instead.
  }
}

function remembered() {
  try {
    return Number(localStorage.getItem(ORG_KEY));
  } catch {
    return 0;
  }
}

// ------------------------------------------------------------------ hash routes

// #project=5&org=2 → { name: "project", value: "5", org: 2 }
function route() {
  const params = new URLSearchParams(location.hash.slice(1));
  const [name, value] = [...params.entries()][0] || ["projects", ""];
  return { name, value, org: Number(params.get("org")) || 0 };
}

function go(hash) {
  if (location.hash === `#${hash}`) render();
  else location.hash = hash;
}

async function render() {
  try {
    state = await api("/api/state");
  } catch (e) {
    show(card(h("p", { class: "error" }, e.message)));
    return;
  }
  document.getElementById("server").textContent = state.server;
  document.title = `${state.server} · DevDash`;
  const who = document.getElementById("who");
  const tabs = document.getElementById("tabs");
  who.replaceChildren();
  const { name, value, org: wanted } = route();

  if (!state.me) {
    tabs.hidden = true;
    if (name === "join" && value) return signupView(value);
    if (name === "signup") return signupView("");
    const params = new URLSearchParams(location.hash.slice(1));
    return loginView(name === "link" ? value : "", params.get("join") || "");
  }

  org = state.orgs.find((o) => o.id === wanted) || state.orgs.find((o) => o.id === remembered()) || state.orgs[0] || null;
  if (org) remember(org.id);
  who.append(orgPicker(), h("button", { class: "link", type: "button", onclick: signOut }, "Sign out"));
  tabs.hidden = !org;
  for (const tab of tabs.querySelectorAll("a")) {
    tab.classList.toggle("on", tab.dataset.tab === name || (name === "project" && tab.dataset.tab === "projects"));
  }
  const invite = new URLSearchParams(location.hash.slice(1)).get("join");
  if (invite) return acceptView(invite);
  if (name === "account") return accountView();
  if (!org) return firstOrgView();
  if (name === "link") return linkView(value);
  if (name === "project") return projectView(Number(value));
  if (name === "teams") return teamsView();
  if (name === "people") return peopleView();
  return projectsView();
}

function orgPicker() {
  if (state.orgs.length < 2) return h("span", { class: "org" }, org ? org.name : state.me.name);
  return h("select", {
    class: "org",
    "aria-label": "Organisation",
    onchange: (event) => {
      remember(event.target.value);
      go(`projects&org=${event.target.value}`);
    },
  }, state.orgs.map((o) => h("option", { value: o.id, selected: org && o.id === org.id }, o.name)));
}

async function signOut() {
  await api("/api/logout", "POST", {});
  go("");
}

// ------------------------------------------------------------------ signing up and in

async function signupView(invite) {
  let about = null;
  if (invite) {
    try {
      about = await api("/api/invites/check", "POST", { invite });
    } catch (e) {
      return show(card(h("h2", {}, "This invite doesn't work"), h("p", {}, e.message),
        h("a", { href: "#login" }, "Sign in instead")));
    }
  } else if (!state.signup) {
    return show(card(h("h2", {}, "Invite only"), h("p", {}, "This server takes new accounts only with an invite link."),
      h("a", { href: "#login" }, "Sign in")));
  }
  show(card(
    h("h2", {}, about ? `Join ${about.org}` : "Make an account"),
    about ? h("p", {}, `You're invited as ${about.role === "admin" ? "an admin" : "a member"}. Pick a username and password.`)
      : h("p", {}, "Then make your organisation and invite your team."),
    form([
      field("Username", "username", "text", { autocomplete: "username", minlength: 2, maxlength: 32 }),
      field("Your name", "name", "text", { autocomplete: "name", maxlength: 60 }),
      field("Password (10 or more characters)", "password", "password", { autocomplete: "new-password", minlength: 10 }),
    ], about ? "Join" : "Make account", async (values) => {
      await api("/api/signup", "POST", { ...values, invite: invite || null });
      // The invite is spent: take it out of the address and the history.
      history.replaceState(null, "", location.pathname + "#projects");
      render();
    }),
    h("p", { class: "muted" }, "Have an account? ",
      h("a", { href: invite ? `#login&join=${encodeURIComponent(invite)}` : "#login" }, "Sign in")),
    certificateHint(),
  ));
}

// On a company network: the server's own root certificate, for a phone or browser to trust once.
function certificateHint() {
  if (!state.root_certificate) return null;
  return h("p", { class: "muted" }, "Browser warned about this site? ",
    h("a", { href: "/root.crt", download: "devdash-team-root.crt" }, "Get this server's certificate"),
    " and install it once (Android: Settings → Security → Install a certificate → CA certificate).");
}

function loginView(linkCode, invite) {
  show(card(
    h("h2", {}, "Sign in"),
    linkCode ? h("p", {}, "Sign in to finish linking your DevDash.") : null,
    invite ? h("p", {}, "Sign in to accept your invite.") : null,
    form([
      field("Username", "username", "text", { autocomplete: "username" }),
      field("Password", "password", "password", { autocomplete: "current-password" }),
    ], "Sign in", async (values) => {
      await api("/api/login", "POST", values);
      render();
    }),
    state.signup ? h("p", { class: "muted" }, "New here? ", h("a", { href: "#signup" }, "Make an account")) : null,
    certificateHint(),
  ));
}

async function acceptView(invite) {
  let about;
  try {
    about = await api("/api/invites/check", "POST", { invite });
  } catch (e) {
    return show(card(h("h2", {}, "This invite doesn't work"), h("p", {}, e.message), h("a", { href: "#projects" }, "Go on")));
  }
  show(card(
    h("h2", {}, `Join ${about.org}?`),
    h("p", {}, `You're invited as ${about.role === "admin" ? "an admin" : "a member"}, signed in as ${state.me.name}.`),
    h("div", { class: "row" },
      action("Join", async () => {
        const joined = await api("/api/invites/accept", "POST", { invite });
        remember(joined.org_id);
        go(`projects&org=${joined.org_id}`);
      }, "primary"),
      h("a", { href: "#projects", class: "button" }, "Not now")),
  ));
}

function newOrgForm() {
  return form([field("Organisation name", "name", "text", { maxlength: 60, placeholder: "Acme Inc" })],
    "Make organisation", async (values) => {
      const made = await api("/api/orgs", "POST", values);
      remember(made.id);
      go(`teams&org=${made.id}`);
    });
}

function firstOrgView() {
  show(card(
    h("h2", {}, "Make your organisation"),
    h("p", {}, "It holds your teams and projects. You'll be its owner and can invite people."),
    newOrgForm(),
    h("p", { class: "muted" }, "Joining someone else's? Open the invite link they sent you."),
  ));
}

async function linkView(code) {
  const done = () => show(card(h("h2", {}, "Linked"),
    h("p", {}, "Your DevDash is linked. You can close this tab and go back to it."),
    h("a", { href: "#projects", class: "button" }, "Go to projects")));
  const ask = (found, entered) => {
    const choose = h("select", { name: "org", "aria-label": "Organisation" },
      state.orgs.map((o) => h("option", { value: o.id, selected: o.id === org.id }, o.name)));
    show(card(
      h("h2", {}, "Link a DevDash"),
      h("p", {}, "A DevDash called ", h("b", {}, found.name), " wants to link to your account. ",
        "It will be able to link projects and share the explanations you choose, as you."),
      h("label", { class: "field" }, h("span", {}, "To organisation"), choose),
      h("p", { class: "muted" }, "Only approve it if you just started linking, and the code on its screen is ",
        h("b", { class: "code" }, entered), "."),
      h("div", { class: "row" },
        action("Approve", async () => {
          await api("/api/device/approve", "POST", { code: entered, org_id: Number(choose.value) });
          history.replaceState(null, "", location.pathname + "#account");
          done();
        }, "primary"),
        h("a", { href: "#projects", class: "button" }, "Cancel")),
    ));
  };
  const enter = (message) => {
    show(card(
      h("h2", {}, "Link a DevDash"),
      form([field("Code shown in DevDash", "code", "text", { placeholder: "ABCD-EFGH", autocomplete: "off" })],
        "Next", async (values) => ask(await api("/api/device/check", "POST", { code: values.code }), values.code)),
    ));
    if (message) main.querySelector(".error").textContent = message;
  };
  if (!code) return enter();
  try {
    ask(await api("/api/device/check", "POST", { code }), code);
  } catch (e) {
    enter(e.message);
  }
}

// ------------------------------------------------------------------ projects

async function projectsView() {
  const projects = await api(here("/projects"));
  show(
    h("h1", {}, "Projects"),
    projects.length === 0
      ? card(h("p", {}, "No projects yet."),
          h("p", { class: "muted" }, "In DevDash on your computer, open Settings → Team server, link it to this server, ",
            "then link a project. Projects others linked show here once a team of yours is added to them."))
      : h("ul", { class: "list" }, projects.map((p) => h("li", {},
          h("a", { href: `#project=${p.id}&org=${org.id}`, class: "item" },
            h("b", {}, p.name),
            h("span", { class: "muted" }, p.shared ? `${p.shared} shared · last ${when(p.last_shared)}` : "Nothing shared yet"))))),
  );
}

async function projectView(id) {
  let project, teams;
  try {
    [project, teams] = await Promise.all([api(here(`/projects/${id}`)), api(here("/teams"))]);
  } catch (e) {
    return show(card(h("p", { class: "error" }, e.message), h("a", { href: "#projects" }, "Back to projects")));
  }
  const reload = () => projectView(id);
  const chips = h("div", { class: "chips" }, project.teams.length === 0
    ? h("span", { class: "muted" }, "No teams yet: only whoever linked it and admins can see it.")
    : project.teams.map((t) => h("span", { class: "chip" }, t.name,
        project.manage ? h("button", {
          type: "button", class: "chip__x", "aria-label": `Remove ${t.name}`,
          onclick: async () => {
            if (!confirm(`Remove ${t.name} from ${project.name}? They'll stop seeing what's shared here.`)) return;
            await api(here(`/projects/${id}/teams/${t.id}`), "DELETE");
            reload();
          },
        }, "×") : null)));
  const free = teams.filter((t) => !project.teams.some((p) => p.id === t.id));
  const adder = project.manage && free.length
    ? h("form", {
        class: "row",
        onsubmit: async (event) => {
          event.preventDefault();
          const team_id = Number(new FormData(event.currentTarget).get("team"));
          await api(here(`/projects/${id}/teams`), "POST", { team_id });
          reload();
        },
      }, h("select", { name: "team", "aria-label": "Team to add" }, free.map((t) => h("option", { value: t.id }, t.name))),
        h("button", { class: "small primary", type: "submit" }, "Add team"))
    : project.manage && teams.length === 0 ? h("p", { class: "muted" }, "Make a team under Teams first.") : null;

  show(
    h("a", { href: "#projects", class: "back" }, "‹ Projects"),
    h("h1", {}, project.name),
    card(h("h2", {}, "Teams on this project"), chips, adder),
    h("h2", { class: "section" }, "Shared explanations"),
    project.shared.length === 0
      ? card(h("p", { class: "muted" }, "Nothing shared yet. In DevDash, press Share with team on an explanation of a change."))
      : project.shared.map((item) => explanationCard(item, reload)),
    project.manage ? h("p", {}, action("Remove this project from the server", async () => {
      if (!confirm(`Remove ${project.name} and everything shared to it from this server? Nothing on anyone's computer changes.`)) return;
      await api(here(`/projects/${id}`), "DELETE");
      go(`projects&org=${org.id}`);
    }, "danger")) : null,
  );
}

function explanationCard(item, reload) {
  const e = item.explanation;
  const list = (title, items, kind) => items && items.length
    ? h("div", { class: kind }, h("h4", {}, title), h("ul", {}, items.map((i) => h("li", {}, i))))
    : null;
  return h("article", { class: "card shared" },
    h("header", {}, h("h3", {}, item.subject), h("span", { class: "muted" }, `${item.person} · ${when(item.shared_at)}`)),
    h("p", {}, e.summary),
    e.change ? h("p", { class: "change" }, h("b", {}, "What changed: "), e.change) : null,
    list("Improves", e.improves, "good"),
    list("Trade-offs", e.tradeoffs, "warn"),
    e.check ? h("p", { class: "check" }, h("b", {}, "Worth checking: "), e.check) : null,
    item.removable
      ? h("div", { class: "row end" }, action("Remove", async () => {
          if (!confirm("Remove this explanation from the server?")) return;
          await api(here(`/shared/${item.id}`), "DELETE");
          reload();
        }, "quiet"))
      : null,
  );
}

// ------------------------------------------------------------------ teams

async function teamsView() {
  const [teams, people] = await Promise.all([api(here("/teams")), api(here("/people"))]);
  const admin = isAdmin();
  show(
    h("h1", {}, "Teams"),
    admin ? card(form([field("New team name", "name", "text", { maxlength: 60 })], "Make team", async (values) => {
      await api(here("/teams"), "POST", values);
      teamsView();
    })) : null,
    teams.length === 0 ? card(h("p", { class: "muted" }, admin
      ? "No teams yet. Make one, invite people into it, then add it to a project."
      : "No teams yet.")) : teams.map((team) => {
      const outside = people.filter((p) => !team.people.some((m) => m.id === p.id));
      return card(
        h("header", { class: "row between" }, h("h2", {}, team.name),
          admin ? action("Delete", async () => {
            if (!confirm(`Delete ${team.name}? Its people stay in the organisation.`)) return;
            await api(here(`/teams/${team.id}`), "DELETE");
            teamsView();
          }, "quiet") : null),
        h("div", { class: "chips" }, team.people.length === 0 ? h("span", { class: "muted" }, "Nobody yet.")
          : team.people.map((m) => h("span", { class: "chip" }, m.name, admin ? h("button", {
              type: "button", class: "chip__x", "aria-label": `Take ${m.name} out of ${team.name}`,
              onclick: async () => {
                await api(here(`/teams/${team.id}/people/${m.id}`), "DELETE");
                teamsView();
              },
            }, "×") : null))),
        admin && outside.length ? h("form", {
          class: "row",
          onsubmit: async (event) => {
            event.preventDefault();
            const person_id = Number(new FormData(event.currentTarget).get("person"));
            await api(here(`/teams/${team.id}/people`), "POST", { person_id });
            teamsView();
          },
        }, h("select", { name: "person", "aria-label": "Person to add" }, outside.map((p) => h("option", { value: p.id }, p.name))),
          h("button", { class: "small primary", type: "submit" }, "Add")) : null,
      );
    }),
  );
}

// ------------------------------------------------------------------ people and invites

async function peopleView() {
  const admin = isAdmin();
  const owner = org.role === "owner";
  const [people, teams, invites] = await Promise.all([
    api(here("/people")), api(here("/teams")), admin ? api(here("/invites")) : Promise.resolve([]),
  ]);
  const made = h("div");
  const inviteForm = admin ? h("form", {
    class: "form",
    onsubmit: async (event) => {
      event.preventDefault();
      const data = new FormData(event.currentTarget);
      const team = data.get("team");
      const { invite } = await api(here("/invites"), "POST", { role: data.get("role"), team_id: team ? Number(team) : null });
      const link = `${location.origin}/#join=${encodeURIComponent(invite)}`;
      const box = h("input", { class: "copy", readonly: true, value: link, "aria-label": "Invite link" });
      made.replaceChildren(card(
        h("p", {}, "Send this link to one person. It works once, for 7 days."),
        box,
        h("div", { class: "row" },
          action("Copy", () => navigator.clipboard.writeText(link), "primary"),
          navigator.share ? action("Share…", () => navigator.share({ title: `Join ${org.name}`, url: link })) : null),
      ));
      box.select();
    },
  },
    h("label", { class: "field" }, h("span", {}, "Role"), h("select", { name: "role" },
      h("option", { value: "member" }, "Member"), owner ? h("option", { value: "admin" }, "Admin") : null)),
    h("label", { class: "field" }, h("span", {}, "Add to team"), h("select", { name: "team" },
      h("option", { value: "" }, "No team yet"), teams.map((t) => h("option", { value: t.id }, t.name)))),
    h("button", { class: "primary", type: "submit" }, "Make invite link"),
  ) : null;

  show(
    h("h1", {}, "People"),
    admin ? card(h("h2", {}, `Invite someone to ${org.name}`), inviteForm, made) : null,
    admin && invites.length ? card(h("h2", {}, "Open invites"), h("ul", { class: "list plain" }, invites.map((i) =>
      h("li", { class: "row between" }, h("span", {}, `${i.role}${i.team ? ` · ${i.team}` : ""} · ends ${when(i.expires_at)}`),
        action("Cancel", async () => {
          await api(here(`/invites/${i.id}`), "DELETE");
          peopleView();
        }, "quiet"))))) : null,
    card(h("ul", { class: "list plain" }, people.map((p) => h("li", { class: "row between" },
      h("span", {}, h("b", {}, p.name), " ", h("span", { class: "muted" }, `@${p.username} · ${p.role}`)),
      h("span", { class: "row" },
        owner && p.role !== "owner" ? action(p.role === "admin" ? "Make member" : "Make admin", async () => {
          await api(here(`/people/${p.id}/role`), "PUT", { role: p.role === "admin" ? "member" : "admin" });
          peopleView();
        }, "quiet") : null,
        admin && p.id !== state.me.id && p.role !== "owner" && (owner || p.role !== "admin") ? action("Remove", async () => {
          if (!confirm(`Remove ${p.name} from ${org.name}? Their linked DevDashes and what they shared here go too.`)) return;
          await api(here(`/people/${p.id}`), "DELETE");
          peopleView();
        }, "danger") : null))))),
  );
}

// ------------------------------------------------------------------ you

async function accountView() {
  const devices = await api("/api/devices");
  show(
    h("h1", {}, "You"),
    card(h("p", {}, h("b", {}, state.me.name), " ", h("span", { class: "muted" }, `@${state.me.username}`))),
    card(h("h2", {}, "Organisations"),
      state.orgs.length ? h("ul", { class: "list plain" }, state.orgs.map((o) => h("li", { class: "row between" },
        h("a", { href: `#projects&org=${o.id}` }, h("b", {}, o.name), " ", h("span", { class: "muted" }, o.role)),
        o.role === "owner"
          ? action("Delete", async () => {
              if (!confirm(`Delete ${o.name}, its teams, projects and everything shared in it? This can't be undone.`)) return;
              await api(`/api/orgs/${o.id}`, "DELETE");
              go("account");
            }, "danger")
          : action("Leave", async () => {
              if (!confirm(`Leave ${o.name}? What you shared there is removed.`)) return;
              await api(`/api/orgs/${o.id}/me`, "DELETE");
              go("account");
            }, "quiet")))) : h("p", { class: "muted" }, "None yet."),
      h("h3", { class: "sub" }, "Make another"), newOrgForm()),
    card(h("h2", {}, "Linked DevDashes"),
      devices.length === 0 ? h("p", { class: "muted" }, "None yet. Link one from Settings → Team server in DevDash.")
        : h("ul", { class: "list plain" }, devices.map((d) => h("li", { class: "row between" },
            h("span", {}, h("b", {}, d.name), " ", h("span", { class: "muted" }, `${d.org} · last used ${when(d.last_seen)}`)),
            action("Unlink", async () => {
              if (!confirm(`Unlink ${d.name}? It can't share until it's linked again.`)) return;
              await api(`/api/devices/${d.id}`, "DELETE");
              accountView();
            }, "danger")))),
      org ? h("p", {}, h("a", { href: "#link" }, "Enter a linking code")) : null),
    card(h("h2", {}, "Change password"), form([
      field("Current password", "current", "password", { autocomplete: "current-password" }),
      field("New password (10 or more characters)", "new", "password", { autocomplete: "new-password", minlength: 10 }),
    ], "Change password", async (values) => {
      await api("/api/password", "PUT", values);
      alert("Password changed. Other browsers were signed out.");
      accountView();
    })),
    serverCard(),
  );
}

// The server's own settings, for whoever runs it.
function serverCard() {
  if (state.me.admin) {
    return card(h("h2", {}, "Server"),
      h("p", {}, state.signup ? "Anyone can make an account." : "New accounts need an invite link."),
      action(state.signup ? "Only with an invite" : "Let anyone sign up", async () => {
        await api("/api/server", "PUT", { signup: state.signup ? "invite" : "open" });
        go("account");
      }));
  }
  if (state.claimed) return null;
  return card(h("h2", {}, "Run this server?"),
    h("p", { class: "muted" }, "Enter the setup code shown where it was started to become its admin."),
    form([field("Setup code", "code", "text", { autocomplete: "one-time-code", placeholder: "ABCD-EFGH-JKLM" })],
      "Become admin", async (values) => {
        await api("/api/setup", "POST", values);
        go("account");
      }));
}

window.addEventListener("hashchange", render);
render();
