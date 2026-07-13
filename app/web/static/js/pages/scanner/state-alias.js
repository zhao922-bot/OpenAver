export function stateAlias() {
    return {
        // ===== Alias State (v2) =====
        aliasCardCollapsed: true,
        aliasRecords: [],            // [{primary_name, aliases, source, created_at, updated_at}]
        aliasRecordsLoading: false,
        aliasRecordsError: '',
        aliasInput: '',              // 單一輸入：篩選 query / 新增主名

        // ===== New Group Form =====
        newGroupAdding: false,

        // ===== Add Alias Inline Input =====
        addingAlias: {},             // { [primary_name]: 'input value' }
        addingAliasLoading: {},      // { [primary_name]: bool }

        // ===== Online Search =====
        onlineSearchName: '',
        onlineSearchLoading: false,
        onlineSearchResults: [],     // suggested_aliases from API
        onlineSearchSelected: [],    // checkbox values
        onlineSearchTarget: '',      // 要加到哪個 primary_name
        onlineSearchDone: false,     // 搜尋完成旗標（用於顯示「無搜尋建議」）

        // ===== Alias v2: Delete Group Modal (T3.5) =====
        deleteAliasGroupModalOpen: false,
        _deleteAliasGroupLoading: false,
        _pendingDeleteAliasGroupName: null,

        // ===== Alias v2: Card Toggle =====
        toggleAliasCard() {
            this.aliasCardCollapsed = !this.aliasCardCollapsed;

            // 首次展開時載入別名列表
            if (!this.aliasCardCollapsed && this.aliasRecords.length === 0) {
                this.loadAliasRecords();
            }
        },

        // ===== Alias v2: Load Records =====
        async loadAliasRecords() {
            this.aliasRecordsLoading = true;
            this.aliasRecordsError = '';

            try {
                const resp = await fetch('/api/actress-aliases');
                const result = await resp.json();

                if (result.success) {
                    this.aliasRecords = result.groups || [];
                } else {
                    this.aliasRecordsError = '載入失敗: ' + (result.error || '未知錯誤');
                }
            } catch (e) {
                this.aliasRecordsError = '載入失敗: ' + e.message;
            } finally {
                this.aliasRecordsLoading = false;
            }
        },

        // ===== Alias v2: Filtered Records (前端 filter) =====
        filteredAliasRecords() {
            const q = this.aliasInput.trim().toLowerCase();
            if (!q) return this.aliasRecords;
            return this.aliasRecords.filter(group => {
                if (group.primary_name.toLowerCase().includes(q)) return true;
                return (group.aliases || []).some(a => a.toLowerCase().includes(q));
            });
        },

        // ===== Alias v2: Create Group =====
        async createAliasGroup() {
            const name = this.aliasInput.trim();
            if (!name) return;

            this.newGroupAdding = true;
            try {
                const resp = await fetch('/api/actress-aliases', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ primary_name: name })
                });
                const result = await resp.json();

                if (result.success) {
                    this.showToast('已新增別名組：' + name, 'success');
                    this.aliasInput = '';
                    await this.loadAliasRecords();
                } else {
                    this.showToast('新增失敗: ' + (result.error || '未知錯誤'), 'error');
                }
            } catch (e) {
                this.showToast('新增失敗: ' + e.message, 'error');
            } finally {
                this.newGroupAdding = false;
            }
        },

        // ===== Alias v2: Delete Group =====
        // T3.5: thin wrapper, 保留外部呼叫名（template @click="deleteAliasGroup(...)" 不需動）
        deleteAliasGroup(name) {
            this.openDeleteAliasGroupModal(name);
        },

        // ===== Alias v2: Delete Group Modal — Open (T3.5) =====
        openDeleteAliasGroupModal(name) {
            this._pendingDeleteAliasGroupName = name;
            this.deleteAliasGroupModalOpen = true;
        },

        // ===== Alias v2: Delete Group Modal — Cancel (T3.5) =====
        cancelDeleteAliasGroupModal() {
            this.deleteAliasGroupModalOpen = false;
            this._pendingDeleteAliasGroupName = null;
        },

        // ===== Alias v2: Delete Group Modal — Confirm (T3.5) =====
        async confirmDeleteAliasGroup() {
            const name = this._pendingDeleteAliasGroupName;
            if (!name) {
                this.deleteAliasGroupModalOpen = false;
                this._pendingDeleteAliasGroupName = null;
                return;
            }
            this._deleteAliasGroupLoading = true;
            try {
                const resp = await fetch(`/api/actress-aliases/${encodeURIComponent(name)}`, {
                    method: 'DELETE'
                });
                const result = await resp.json();

                if (result.success) {
                    this.showToast('已刪除：' + name, 'success');
                    await this.loadAliasRecords();
                } else {
                    this.showToast('刪除失敗: ' + (result.error || '未知錯誤'), 'error');
                }
            } catch (e) {
                this.showToast('刪除失敗: ' + e.message, 'error');
            } finally {
                this._deleteAliasGroupLoading = false;
                this.deleteAliasGroupModalOpen = false;
                this._pendingDeleteAliasGroupName = null;
            }
        },

        // ===== Alias v2: Show Add Alias Inline Input =====
        showAddAliasInput(primary) {
            this.addingAlias = { ...this.addingAlias, [primary]: '' };
        },

        // ===== Alias v2: Cancel Add Alias Inline Input =====
        cancelAddAlias(primary) {
            const updated = { ...this.addingAlias };
            delete updated[primary];
            this.addingAlias = updated;
        },

        // ===== Alias v2: Add Alias to Group (Enter key) =====
        async addAlias(primary) {
            const alias = (this.addingAlias[primary] || '').trim();
            if (!alias) return;

            this.addingAliasLoading = { ...this.addingAliasLoading, [primary]: true };
            try {
                const resp = await fetch(`/api/actress-aliases/${encodeURIComponent(primary)}/alias`, {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ alias })
                });
                const result = await resp.json();

                if (result.success) {
                    // 清除 inline input
                    const updated = { ...this.addingAlias };
                    delete updated[primary];
                    this.addingAlias = updated;
                    await this.loadAliasRecords();
                } else {
                    this.showToast(result.error || '新增別名失敗', 'error');
                }
            } catch (e) {
                this.showToast('新增別名失敗: ' + e.message, 'error');
            } finally {
                const loading = { ...this.addingAliasLoading };
                delete loading[primary];
                this.addingAliasLoading = loading;
            }
        },

        // ===== Alias v2: Remove Alias from Group =====
        async removeAlias(primary, alias) {
            try {
                const resp = await fetch(
                    `/api/actress-aliases/${encodeURIComponent(primary)}/alias/${encodeURIComponent(alias)}`,
                    { method: 'DELETE' }
                );
                const result = await resp.json();

                if (result.success) {
                    await this.loadAliasRecords();
                } else {
                    this.showToast('移除失敗: ' + (result.error || '未知錯誤'), 'error');
                }
            } catch (e) {
                this.showToast('移除失敗: ' + e.message, 'error');
            }
        },

        // ===== Alias v2: Start Online Search =====
        async startOnlineSearch() {
            const name = this.aliasInput.trim();
            if (!name) {
                this.showToast('請先輸入主名稱再進行線上搜尋');
                return;
            }

            this.onlineSearchTarget = name;
            this.onlineSearchLoading = true;
            this.onlineSearchResults = [];
            this.onlineSearchSelected = [];
            this.onlineSearchDone = false;

            try {
                const resp = await fetch('/api/actress-aliases/search-online', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ name })
                });
                if (resp.status === 504) {
                    this.showToast('線上搜尋逾時', 'error');
                    return;
                }
                const result = await resp.json();

                if (result.success) {
                    this.onlineSearchResults = result.suggested_aliases || [];
                } else {
                    this.showToast('線上搜尋失敗: ' + (result.error || '未知錯誤'), 'error');
                }
            } catch (e) {
                this.showToast('線上搜尋失敗: ' + e.message, 'error');
            } finally {
                this.onlineSearchLoading = false;
                this.onlineSearchDone = true;
            }
        },

        // ===== Alias v2: Confirm Online Search Selection =====
        async confirmOnlineSearch(primary) {
            const selected = [...this.onlineSearchSelected];
            if (selected.length === 0) return;

            // 確保 primary group 存在
            const exists = this.aliasRecords.some(g => g.primary_name === primary);
            if (!exists) {
                // 先建立 group
                const createResp = await fetch('/api/actress-aliases', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ primary_name: primary })
                });
                const createResult = await createResp.json();
                if (!createResult.success) {
                    this.showToast('建立別名組失敗: ' + (createResult.error || '未知錯誤'), 'error');
                    return;
                }
            }

            // 逐一加入選中的 alias
            for (const alias of selected) {
                try {
                    const resp = await fetch(`/api/actress-aliases/${encodeURIComponent(primary)}/alias`, {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ alias })
                    });
                    const result = await resp.json();
                    if (!result.success) {
                        this.showToast(result.error || `加入 ${alias} 失敗`, 'error');
                    }
                } catch (e) {
                    this.showToast(`加入 ${alias} 失敗: ` + e.message, 'error');
                }
            }

            // 清空線上搜尋結果
            this.onlineSearchResults = [];
            this.onlineSearchSelected = [];
            this.onlineSearchTarget = '';
            this.onlineSearchDone = false;
            await this.loadAliasRecords();
        },
    };
}
