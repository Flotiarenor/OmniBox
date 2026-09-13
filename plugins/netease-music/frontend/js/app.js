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
        content.innerHTML = '<div class="empty">✅ 已登录网易云音乐</div>';
        return;
      }
    } catch (e) {}
    content.innerHTML = `<div class="empty">未登录<br><br>请在终端执行：<br><b>ncm-cli configure</b><br><b>ncm-cli login</b><br><br><button class="btn btn-primary" id="refresh">我已登录</button></div>`;
    document.getElementById('refresh').addEventListener('click', () => this.renderLogin());
  }

  renderSongs(songs) {
    const content = document.getElementById('content');
    if (!songs.length) { content.innerHTML = '<div class="empty">暂无歌曲</div>'; return; }
    content.innerHTML = songs.map((s, i) => `
      <div class="item" data-idx="${i}">
        <span>🎵</span>
        <div><div class="t">${this.esc(s.name)}</div><div class="s">${this.esc((s.artists || []).join(', '))}</div></div>
      </div>`).join('');
    content.querySelectorAll('.item').forEach(el => {
      el.addEventListener('click', async () => {
        const song = songs[parseInt(el.dataset.idx, 10)];
        const urlData = await this.call('get_song_url', song.original_id);
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
    if (!playlists.length) { content.innerHTML = '<div class="empty">暂无歌单</div>'; return; }
    content.innerHTML = playlists.map(p => `
      <div class="item">
        <span>📋</span>
        <div><div class="t">${this.esc(p.name)}</div><div class="s">${p.track_count} 首</div></div>
      </div>`).join('');
  }

  esc(str) {
    return String(str == null ? '' : str).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  }
}
