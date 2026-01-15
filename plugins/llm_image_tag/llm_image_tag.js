(function () {
  'use strict';

  const PLUGIN_IDS = ['llm_image_tag', 'LLMImageTag'];
  const MENU_ITEM_ID = 'llm-image-tag-menu-item';
  const OPERATIONS_TOGGLE_ID = 'operation-menu';

  function getImageIdFromURL() {
    try {
      const pathMatch = window.location.pathname.match(/\/images\/(\d+)/);
      if (pathMatch) return parseInt(pathMatch[1], 10);
      const hashMatch = window.location.hash.match(/\/images\/(\d+)/);
      if (hashMatch) return parseInt(hashMatch[1], 10);
    } catch (e) {
      console.warn('[LLMImageTag] Failed to parse image id from URL:', e);
    }
    return undefined;
  }

  function getBaseURL() {
    const base = document.querySelector('base')?.getAttribute('href') || '/';
    return new URL(base, window.location.href);
  }

  function getGraphqlURL() {
    return new URL('graphql', getBaseURL()).toString();
  }

  function getPluginAssetURL(pluginId, assetPath) {
    return new URL(
      `plugin/${pluginId}/assets/${assetPath}`,
      getBaseURL()
    ).toString();
  }

  async function graphqlRequest(graphqlURL, query, variables) {
    const res = await fetch(graphqlURL, {
      method: 'POST',
      credentials: 'include',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ query, variables }),
    });
    if (!res.ok) throw new Error(`HTTP ${res.status} ${res.statusText}`);
    const json = await res.json();
    if (json.errors?.length) {
      const msg = json.errors.map((e) => e.message).join('; ');
      throw new Error(msg || 'GraphQL error');
    }
    return json.data;
  }

  async function resolvePluginId(graphqlURL) {
    const query = `query { plugins { id name } }`;
    try {
      const data = await graphqlRequest(graphqlURL, query);
      if (!data || !data.plugins) return null;

      const plugins = data.plugins;
      for (const p of plugins) {
        if (PLUGIN_IDS.includes(p.id)) return p.id;
      }
      for (const p of plugins) {
        if (PLUGIN_IDS.includes(p.name)) return p.id;
      }
      for (const p of plugins) {
        const n = (p.name || '').toLowerCase();
        const i = (p.id || '').toLowerCase();
        if (n.includes('llm') && n.includes('tag')) return p.id;
        if (i.includes('llm') && i.includes('tag')) return p.id;
      }
      return null;
    } catch (e) {
      console.error('[LLMImageTag] Failed to resolve plugin id:', e);
      return null;
    }
  }


  function sleep(ms) {
    return new Promise((resolve) => setTimeout(resolve, ms));
  }

  async function waitForJobComplete(graphqlURL, jobId, isCancelled) {
    const query = `
      query FindJob($input: FindJobInput!) {
        findJob(input: $input) { status error }
      }
    `;
    const intervalMs = 1000;
    while (!isCancelled()) {
      const data = await graphqlRequest(graphqlURL, query, {
        input: { id: jobId },
      });
      const job = data?.findJob;
      if (job?.status === 'FINISHED') return job;
      if (job?.status === 'FAILED') {
        throw new Error(job?.error || 'LLM task failed.');
      }
      if (job?.status === 'CANCELLED') {
        throw new Error('LLM task cancelled.');
      }
      await sleep(intervalMs);
    }
    throw new Error('LLM task cancelled.');
  }

  async function waitForTagResults(pluginId, imageId, requestId, isCancelled) {
    const url = getPluginAssetURL(
      pluginId,
      `results/${imageId}_${requestId}.json`
    );
    const intervalMs = 500;
    while (!isCancelled()) {
      const res = await fetch(url, {
        credentials: 'include',
        cache: 'no-store',
      });
      if (res.ok) {
        try {
          return await res.json();
        } catch (e) {
          throw new Error('Failed to parse LLM tag results.');
        }
      }
      if (res.status && res.status !== 404) {
        throw new Error(
          `Failed to fetch LLM tag results (HTTP ${res.status})`
        );
      }
      await sleep(intervalMs);
    }
    throw new Error('LLM task cancelled.');
  }

  async function findImageTagIds(graphqlURL, imageId) {
    const query = `
      query FindImage($id: ID!) {
        findImage(id: $id) {
          tags { id name }
        }
      }
    `;
    const data = await graphqlRequest(graphqlURL, query, { id: imageId });
    const tags = data?.findImage?.tags ?? [];
    return tags.map((t) => t.id);
  }

  async function findTagMatch(graphqlURL, name) {
    const query = `
      query FindTags($filter: FindFilterType) {
        findTags(filter: $filter) {
          tags { id name aliases }
        }
      }
    `;
    const data = await graphqlRequest(graphqlURL, query, {
      filter: { q: name, per_page: 25 },
    });
    const tags = data?.findTags?.tags ?? [];
    const needle = name.trim().toLowerCase();
    const exact = tags.find((t) => (t.name || '').toLowerCase() === needle) || null;
    const alias = tags.find((t) =>
      (t.aliases || []).some((a) => (a || '').toLowerCase() === needle)
    ) || null;
    return { exact, alias };
  }

  async function createTagSafe(graphqlURL, name, aliasOwner) {
    try {
      const created = await createTag(graphqlURL, name);
      return { id: created?.id || null, created: true };
    } catch (e) {
      console.error('[LLMImageTag] Tag create failed:', {
        name,
        error: e?.message || String(e),
      });
      if (aliasOwner?.id) {
        console.error('[LLMImageTag] Falling back to alias owner tag:', {
          name,
          aliasOwner: aliasOwner.name,
          aliasOwnerId: aliasOwner.id,
        });
        return { id: aliasOwner.id, created: false };
      }
      return { id: null, created: false, error: e };
    }
  }

  async function createTag(graphqlURL, name) {
    const mutation = `
      mutation TagCreate($input: TagCreateInput!) {
        tagCreate(input: $input) { id name }
      }
    `;
    const data = await graphqlRequest(graphqlURL, mutation, {
      input: { name },
    });
    return data?.tagCreate;
  }

  async function updateImageTags(graphqlURL, imageId, tagIds) {
    const mutation = `
      mutation ImageUpdate($input: ImageUpdateInput!) {
        imageUpdate(input: $input) { id }
      }
    `;
    console.debug('[LLMImageTag] Updating image tags:', {
      imageId,
      tagCount: tagIds.length,
      tagIds,
    });
    await graphqlRequest(graphqlURL, mutation, {
      input: { id: imageId, tag_ids: tagIds },
    });
  }

  async function runTag(imageId) {
    const mutation = `
      mutation RunPluginTask($plugin_id: ID!, $args_map: Map!) {
        runPluginTask(plugin_id: $plugin_id, args_map: $args_map)
      }
    `;
    const requestId = `req_${Date.now()}_${Math.random()
      .toString(36)
      .slice(2, 10)}`;
    const args_map = {
      mode: 'tag_image_task',
      image_id: imageId,
      request_id: requestId,
    };
    const graphqlURL = getGraphqlURL();

    const resolvedId = await resolvePluginId(graphqlURL);
    if (!resolvedId) {
      console.error('[LLMImageTag] Could not resolve plugin id. Aborting to avoid server error.');
      alert('LLM Image Tag plugin not found on server. Try reloading plugins and refreshing the page.');
      return null;
    }

    try {
      const res = await fetch(graphqlURL, {
        method: 'POST',
        credentials: 'include',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ query: mutation, variables: { plugin_id: resolvedId, args_map } }),
      });
      if (!res.ok) throw new Error(`HTTP ${res.status} ${res.statusText}`);
      const json = await res.json();
      if (json.errors) {
        console.error('[LLMImageTag] GraphQL errors:', json.errors);
        alert('Failed to start image tagging. See console for details.');
        return null;
      }
      const jobId = json.data?.runPluginTask || null;
      console.debug('[LLMImageTag] Tagging queued as job:', jobId);
      return { jobId, pluginId: resolvedId, requestId };
    } catch (e) {
      console.error('[LLMImageTag] Request failed:', e);
      alert('Failed to start image tagging. See console for details.');
      return null;
    }
  }

  function openTagModal(imageId) {
    const PluginApi = window.PluginApi;
    if (!PluginApi?.React || !PluginApi?.ReactDOM) {
      alert('LLM Image Tag: PluginApi is not available in this UI context.');
      return false;
    }
    if (!PluginApi?.libraries?.Bootstrap) {
      alert('LLM Image Tag: Bootstrap components are not available.');
      return false;
    }

    const React = PluginApi.React;
    const ReactDOM = PluginApi.ReactDOM;
    const { Modal, Button, Form, Spinner, Badge } =
      PluginApi.libraries.Bootstrap;
    if (!Modal || !Button || !Form || !Spinner || !Badge) {
      alert('LLM Image Tag: Required UI components are missing.');
      return false;
    }

    const container = document.createElement('div');
    document.body.appendChild(container);

    function TagModal() {
      const { useEffect, useMemo, useState } = React;
      let Toast = null;
      try {
        Toast = PluginApi.hooks?.useToast ? PluginApi.hooks.useToast() : null;
      } catch (e) {
        Toast = null;
      }
      const [loading, setLoading] = useState(true);
      const [applying, setApplying] = useState(false);
      const [error, setError] = useState('');
      const [suggestions, setSuggestions] = useState([]);
      const [selected, setSelected] = useState([]);

      const graphqlURL = useMemo(() => getGraphqlURL(), []);

      useEffect(() => {
        let cancelled = false;
        async function load() {
          setLoading(true);
          setError('');
          try {
            const job = await runTag(imageId);
            if (!job?.pluginId || !job?.requestId) {
              throw new Error('Failed to queue LLM tagging task.');
            }
            if (job.jobId) {
              await waitForJobComplete(
                graphqlURL,
                job.jobId,
                () => cancelled
              );
            }
            const result = await waitForTagResults(
              job.pluginId,
              imageId,
              job.requestId,
              () => cancelled
            );
            if (result?.error) {
              throw new Error(result.error);
            }
            const tagNames = (result?.tags || [])
              .map((t) => (t || '').trim())
              .filter((t) => t);
            if (!tagNames.length) {
              throw new Error('No tags returned from LLM task.');
            }
            const enriched = [];
            for (const name of tagNames) {
              const match = await findTagMatch(graphqlURL, name);
              const aliasMatch = !match.exact && match.alias;
              if (aliasMatch) {
                console.debug('[LLMImageTag] Alias match resolved:', {
                  name,
                  aliasOwner: match.alias.name,
                  aliasOwnerId: match.alias.id,
                });
                continue;
              }
              enriched.push({
                uid: `${name}::${match.exact?.id || 'new'}::${enriched.length}`,
                name,
                existingId: match.exact?.id || null,
                aliasOwnerId: null,
                aliasOwnerName: null,
                isAlias: false,
              });
            }
            if (cancelled) return;
            setSuggestions(enriched);
            setSelected(enriched.map((t) => t.uid));
            console.error('[LLMImageTag] Suggestions loaded:', enriched);
          } catch (e) {
            if (cancelled) return;
            setError(e?.message || String(e));
          } finally {
            if (!cancelled) setLoading(false);
          }
        }

        load();
        return () => {
          cancelled = true;
        };
      }, [graphqlURL]);

      function toggleSelected(uid) {
        setSelected((prev) => {
          if (prev.includes(uid)) {
            return prev.filter((t) => t !== uid);
          }
          return prev.concat([uid]);
        });
      }

      async function applyTags() {
        if (!selected.length) {
          Toast?.info?.('No tags selected');
          return;
        }
        setApplying(true);
        setError('');
        try {
          const currentTagIds = await findImageTagIds(graphqlURL, imageId);
          const tagIds = currentTagIds.slice();
          console.debug('[LLMImageTag] Current image tags:', currentTagIds);
          console.debug('[LLMImageTag] Selected tags:', selected);
          for (const suggestion of suggestions) {
            if (!selected.includes(suggestion.uid)) continue;
            let tagId = suggestion.existingId;
            if (!tagId) {
              console.debug('[LLMImageTag] Creating tag:', suggestion.name);
              const created = await createTagSafe(graphqlURL, suggestion.name, {
                id: suggestion.aliasOwnerId,
                name: suggestion.aliasOwnerName,
              });
              tagId = created.id;
              console.debug('[LLMImageTag] Created tag:', {
                name: suggestion.name,
                id: tagId,
                created: created.created,
              });
            }
            console.debug('[LLMImageTag] Using tag:', {
              name: suggestion.name,
              id: tagId,
            });
            if (tagId && !tagIds.includes(tagId)) {
              tagIds.push(tagId);
            }
          }
          const deduped = Array.from(new Set(tagIds));
          console.debug('[LLMImageTag] Final tag id list:', deduped);
          await updateImageTags(graphqlURL, imageId, deduped);
          const after = await findImageTagIds(graphqlURL, imageId);
          console.debug('[LLMImageTag] Tags after update:', after);
          Toast?.success?.('Tags applied');
          onClose();
        } catch (e) {
          setError(e?.message || String(e));
          Toast?.error?.(e);
        } finally {
          setApplying(false);
        }
      }

      const empty = !loading && !suggestions.length;

      return (
        React.createElement(
          Modal,
          { show: true, onHide: onClose, size: 'lg' },
          React.createElement(
            Modal.Header,
            { closeButton: true },
            React.createElement(Modal.Title, null, 'LLM Suggested Tags')
          ),
          React.createElement(
            Modal.Body,
            null,
            loading
              ? React.createElement(
                  'div',
                  { className: 'd-flex align-items-center' },
                  React.createElement(Spinner, {
                    animation: 'border',
                    role: 'status',
                    className: 'mr-3',
                  }),
                  React.createElement('span', null, 'Running LLM task...')
                )
              : null,
            error
              ? React.createElement(
                  'div',
                  { className: 'text-danger mb-3' },
                  error
                )
              : null,
            empty
              ? React.createElement(
                  'div',
                  { className: 'text-muted' },
                  'No tag suggestions available.'
                )
              : null,
            !loading && suggestions.length
              ? React.createElement(
                  'div',
                  null,
                  React.createElement(
                    'div',
                    { className: 'mb-2 d-flex align-items-center' },
                    React.createElement(
                      Button,
                      {
                        variant: 'secondary',
                        size: 'sm',
                        className: 'mr-2',
                        onClick: () =>
                          setSelected(suggestions.map((t) => t.uid)),
                      },
                      'Select all'
                    ),
                    React.createElement(
                      Button,
                      {
                        variant: 'secondary',
                        size: 'sm',
                        onClick: () => setSelected([]),
                      },
                      'Clear'
                    )
                  ),
                  suggestions.map((t) =>
                    React.createElement(
                      Form.Check,
                      {
                        key: t.uid,
                        type: 'checkbox',
                        className: 'mb-2',
                        checked: selected.includes(t.uid),
                        onChange: () => toggleSelected(t.uid),
                        label: React.createElement(
                          'span',
                          null,
                          t.name,
                          t.existingId
                            ? React.createElement(
                                Badge,
                                { variant: 'secondary', className: 'ml-2' },
                                t.isAlias && t.aliasOwnerName
                                  ? `alias of ${t.aliasOwnerName}`
                                  : 'existing'
                              )
                            : React.createElement(
                                Badge,
                                { variant: 'secondary', className: 'ml-2' },
                                'new'
                              )
                        ),
                      }
                    )
                  )
                )
              : null
          ),
          React.createElement(
            Modal.Footer,
            null,
            React.createElement(
              Button,
              { variant: 'secondary', onClick: onClose, disabled: applying },
              'Cancel'
            ),
            React.createElement(
              Button,
              {
                variant: 'primary',
                onClick: applyTags,
                disabled: applying || loading || !selected.length,
              },
              applying ? 'Applying...' : 'Apply Tags'
            )
          )
        )
      );
    }

    function onClose() {
      ReactDOM.unmountComponentAtNode(container);
      container.remove();
    }

    try {
      ReactDOM.render(React.createElement(TagModal), container);
    } catch (e) {
      console.error('[LLMImageTag] Failed to render modal:', e);
      alert(
        `LLM Image Tag: failed to render modal. ${e?.message || String(e)}`
      );
      onClose();
      return false;
    }
    return true;
  }

  function closeDropdown(menuEl) {
    const dropdown = menuEl?.closest('.dropdown');
    menuEl?.classList.remove('show');
    dropdown?.classList.remove('show');
  }

  function createMenuItem(menuEl) {
    if (!menuEl) return;
    const existing = document.getElementById(MENU_ITEM_ID);
    if (existing) {
      if (menuEl.contains(existing)) return;
      existing.remove();
    }

    const item = document.createElement('button');
    item.id = MENU_ITEM_ID;
    item.type = 'button';
    item.className = 'dropdown-item bg-secondary text-white';
    item.textContent = 'Tag image (LLM)';
    item.addEventListener('click', function (ev) {
      ev.preventDefault();
      console.debug('[LLMImageTag] Menu item clicked');
      const imageId = getImageIdFromURL();
      if (!imageId) {
        alert('LLM Image Tag: could not determine image id from URL.');
        return;
      }
      if (!openTagModal(imageId)) {
        runTag(imageId);
      }
      closeDropdown(menuEl);
    });
    item.style.cursor = 'pointer';

    const items = Array.from(menuEl.querySelectorAll('.dropdown-item'));
    const defaultThumbItem = items.find((el) => {
      const text = (el.textContent || '').trim().toLowerCase();
      return text.includes('generate default thumbnail');
    });
    if (defaultThumbItem?.parentElement === menuEl) {
      defaultThumbItem.insertAdjacentElement('afterend', item);
    } else {
      const deleteItem = items.find((el) => {
        const text = (el.textContent || '').trim().toLowerCase();
        return text.includes('delete');
      });
      if (deleteItem?.parentElement === menuEl) {
        menuEl.insertBefore(item, deleteItem);
      } else {
        menuEl.appendChild(item);
      }
    }
  }

  function findOperationsMenu() {
    const toggle = document.getElementById(OPERATIONS_TOGGLE_ID);
    if (!toggle) return null;
    const dropdown = toggle.closest('.dropdown');
    if (!dropdown) return null;
    const menuEl = dropdown.querySelector('.dropdown-menu');
    if (!menuEl) return null;
    return menuEl;
  }

  function mountIfPossible() {
    if (!getImageIdFromURL()) return false;
    const menuEl = findOperationsMenu();
    if (!menuEl) return false;
    createMenuItem(menuEl);
    return true;
  }

  if (typeof window.registerTask === 'function') {
    window.registerTask({
      name: 'Tag image (LLM)',
      description: 'Tag the current image using a vision LLM',
      icon: 'fa-tags',
      handler: async () => {
        const imageId = getImageIdFromURL();
        if (!imageId) {
          alert('LLM Image Tag: could not determine image id from URL.');
          return;
        }
        if (!openTagModal(imageId)) {
          await runTag(imageId);
        }
      },
    });
    console.debug('[LLMImageTag] Task registered via registerTask');
  } else {
    mountIfPossible();
    const observer = new MutationObserver((mutationsList) => {
      for (const mutation of mutationsList) {
        for (const addedNode of mutation.addedNodes) {
          if (addedNode.nodeType !== Node.ELEMENT_NODE) continue;
          if (
            addedNode.id === OPERATIONS_TOGGLE_ID ||
            addedNode.querySelector?.(`#${OPERATIONS_TOGGLE_ID}`) ||
            addedNode.classList?.contains('dropdown-menu')
          ) {
            mountIfPossible();
            return;
          }
        }
      }
    });
    observer.observe(document.body, { childList: true, subtree: true });
  }

  console.debug('[LLMImageTag] UI script initialized');
})();
