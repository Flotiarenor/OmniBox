// 图标走壳的图标集（引导脚本已内联 sprite）。`Icons` 缺失时返回空串，不写 emoji 兜底
// （emoji 字面量会让 check_plugins 的 emoji 门禁永远清不掉）。
// 本插件的 5 个原生视图实际由 media-player 渲染，这份页面只在直接打开时才用到。
const icon = (name) => (window.Icons && typeof window.Icons.html === 'function')
  ? window.Icons.html(name)
  : '';

class NeteaseApp {
  constructor() {
    this.view = 'daily';
    this.items = [];
  }

  init() {
    const params = new URLSearchParams(location.search);
    this.view = params.get('view') || 'daily';
    document.getElementById('title').textContent = {
      daily: '每日推荐', playlists: '推荐歌单', liked: '我的喜欢', login: '登录'
    }[this.view] || '网易云音乐';
    this._mountFreshness();

    if (this.view === 'playlists') {
      document.getElementById('search-row').style.display = 'block';
      document.getElementById('search-btn').addEventListener('click', () => this.searchPlaylists());
      document.getElementById('keyword').addEventListener('keydown', e => { if (e.key === 'Enter') this.searchPlaylists(); });
      this.loadPlaylists('推荐');
    } else if (this.view === 'liked') {
      this.loadLiked();
    } else if (this.view === 'login') {
      this.renderLogin();
    } else {
      document.getElementById('search-row').style.display = 'block';
      document.getElementById('search-btn').addEventListener('click', () => this.searchSongs());
      document.getElementById('keyword').addEventListener('keydown', e => { if (e.key === 'Enter') this.searchSongs(); });
      this.loadDaily();
    }
  }

  async call(method, ...args) {
    return await Bridge.call(method, ...args);
  }

  /**
   * 挂载共享的「同步状态 + 校验」控件（shell/freshness.js，见 plugin-guide §3.4）。
   *
   * 本插件是**远端来源**：没有本地文件树，本地只有一份短期播放地址缓存。
   * 状态行显示缓存新鲜期，「校验」立即丢弃缓存（下次取地址重新解析）。
   */
  _mountFreshness() {
    const host = document.getElementById('ncm-freshness');
    if (!host || typeof Freshness === 'undefined') return;
    this.freshness = Freshness.mount({
      plugin: 'netease-music',
      container: host,
      unit: '首',
      onChange: () => this._reloadView(),
    });
    if (this.freshness && typeof this.freshness.autoSync === 'function') {
      this.freshness.autoSync('');
    }
  }

  /** 校验完成 / 缓存被丢弃后，重新拉一次当前视图的数据。 */
  _reloadView() {
    if (this.view === 'playlists') this.loadPlaylists('推荐');
    else if (this.view === 'liked') this.loadLiked();
    else if (this.view === 'login') this.renderLogin();
    else this.loadDaily();
  }

  async loadDaily() {
    const data = await this.call('get_daily_recommend');
    this.renderSongs(data.results || []);
  }

  async searchSongs() {
    const kw = document.getElementById('keyword').value.trim();
    if (!kw) return;
    const data = await this.call('search_song', kw);
    this.renderSongs(data.results || []);
  }

  async loadLiked() {
    const data = await this.call('get_liked_songs', 100);
    this.renderSongs(data.results || []);
  }

  async loadPlaylists(kw) {
    const data = await this.call('search_playlist', kw);
    this.renderPlaylists(data.results || []);
  }

  async searchPlaylists() {
    const kw = document.getElementById('keyword').value.trim();
    if (!kw) return;
    await this.loadPlaylists(kw);
  }

  async renderLogin() {
    const content = document.getElementById('content');
    try {
      const login = await this.call('check_login');
      if (login.success) {
        content.innerHTML = '<div class="empty-state">' + icon('icon:circle-check') + ' 已登录网易云音乐</div>';
        return;
      }
    } catch (e) {}
    content.innerHTML = `<div class="empty-state">未登录<br><br>请在终端执行：<br><b>ncm-cli configure</b><br><b>ncm-cli login</b><br><br><button class="btn btn-primary" id="refresh">我已登录</button></div>`;
    document.getElementById('refresh').addEventListener('click', () => this.renderLogin());
  }

  renderSongs(songs) {
    const content = document.getElementById('content');
    if (!songs.length) { content.innerHTML = '<div class="empty-state">暂无歌曲</div>'; return; }
    content.innerHTML = songs.map((s, i) => `
      <div class="item" data-idx="${i}">
        <span>${icon('icon:music')}</span>
        <div><div class="t">${this.esc(s.name)}</div><div class="s">${this.esc((s.artists || []).join(', '))}</div></div>
      </div>`).join('');
    content.querySelectorAll('.item').forEach(el => {
      el.addEventListener('click', async () => {
        const song = songs[parseInt(el.dataset.idx, 10)];
        // 两个 id 都要传：后端 `get_song_url(song_id, original_id)` 只有在拿到
        // original_id 时才能拼公共外链，少传一个就恒返回 None（点歌必报"获取播放
        // 地址失败"）。此前这里只传了 `song.original_id`。
        const urlData = await this.call('get_song_url', song.id, song.original_id);
        if (!urlData || !urlData.url) { Toast.error('获取播放地址失败'); return; }
        const media = parent && parent.mediaPlayerApp;
        const item = {
          id: 'ncm:' + song.original_id,
          original_id: song.original_id,
          kind: 'audio',
          title: song.name,
          artist: (song.artists || []).join(', '),
          album: song.album || '',
          duration: (song.duration || 0) / 1000,
          path: '',
          stream_url: urlData.url,
          online: true
        };
        if (media && media.core) {
          media.core.setQueue([item], 0);
        } else {
          Toast.error('无法访问 media-player 播放器');
        }
      });
    });
  }

  renderPlaylists(playlists) {
    const content = document.getElementById('content');
    if (!playlists.length) { content.innerHTML = '<div class="empty-state">暂无歌单</div>'; return; }
    content.innerHTML = playlists.map(p => `
      <div class="item">
        <span>${icon('icon:list-music')}</span>
        <div><div class="t">${this.esc(p.name)}</div><div class="s">${p.track_count} 首</div></div>
      </div>`).join('');
  }

  esc(str) {
    return String(str == null ? '' : str).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  }
}
