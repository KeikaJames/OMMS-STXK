// 社团卡片图：管理员在编辑页为每个社团选一幅插图（存为 club.art），或上传照片（club.image_path，优先显示）。
// 插图是单线手绘风格，线条跟随文字颜色，底色按社团编号轮换。只用 ES5。
var ClubArt = (function () {
  'use strict';
  function svg(body) {
    return '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 240 150" preserveAspectRatio="xMidYMid slice" fill="none" ' +
      'stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" focusable="false">' +
      body + '</svg>';
  }

  // 顺序即编辑页的排列顺序；键名与 main.py 的 CLUB_ART 一致。
  var options = [
    ['club', '通用'],
    ['laser', '激光雕刻'],
    ['design', '平面设计'],
    ['model3d', '3D 建模'],
    ['print', '3D 打印'],
    ['media', '影视传媒'],
    ['wood', '木工'],
    ['code', '编程'],
    ['robot', '机器人'],
    ['paint', '美术'],
    ['music', '音乐'],
    ['science', '科学实验'],
    ['sports', '体育'],
    ['book', '阅读写作']
  ];

  var art = {
    club: '<path d="M104 124 L104 30"/><circle cx="104" cy="26" r="3.2"/><path d="M104 33 C 122 28.5, 140 40, 162 34.5 L162 67 C 140 72.5, 122 61, 104 65.5"/><path d="M115 50 C 124 47, 134 53, 146 50" stroke-width="1.6"/><path d="M86 126 C 96 123.5, 112 123.5, 122 126"/>',
    print: '<path d="M68 128 L172 128 L168 120 L72 120 Z"/><path d="M80 120 L80.6 27"/><path d="M160 120 L159.4 27"/><path d="M73 26.5 C 100 25.6, 140 25.6, 167 26.5"/><path d="M80 54 L160 53.4"/><path d="M110 45 H130 V61 H110 Z"/><path d="M116 61 L124 61 L120 68 Z" fill="currentColor"/><path d="M88 106 C 108 105.4, 132 105.4, 152 106"/><path d="M92 110.5 H148"/><path d="M108.5 105.5 C 106 98, 104.5 92, 110.5 85.5 L129.5 85.5 C 135.5 92, 134 98, 131.5 105.5"/><path d="M107 99 H133"/><path d="M106.6 92.5 H133.4"/><circle cx="180" cy="40" r="11"/><circle cx="180" cy="40" r="3.5"/><path d="M160 40 H169"/><path d="M184 50.5 C 182 66, 150 66, 127 45" stroke-width="1.6"/>',
    media: '<path d="M60 64 H144 V120 H60 Z"/><path d="M60 74 H144"/><path d="M74 64 L68 74"/><path d="M92 64 L86 74"/><path d="M110 64 L104 74"/><path d="M128 64 L122 74"/><path d="M58.5 60.5 L142.5 44.5 L145 54.5 L61 70.5 Z"/><path d="M76 57 L82 66.5"/><path d="M94 53.5 L100 63"/><path d="M112 50 L118 59.5"/><path d="M130 46.8 L136 56"/><path d="M70 88 H134"/><path d="M70 100 H112"/><path d="M70 111 H124"/><path d="M163 41 C163 31 183 31 183 41 L183 61 C183 71 163 71 163 61 Z"/><path d="M163 47 H183"/><path d="M163 53 H183"/><path d="M163 59 H183"/><path d="M157 57 C157 79 189 79 189 57"/><path d="M173 74 L173 118"/><path d="M160 121 C166 118.5 180 118.5 186 121"/>',
    wood: '<path d="M48 95 L188 88.5 L189 102.5 L49 109 Z"/><path d="M60 102 C 78 98, 94 104, 114 99.5"/><path d="M128 97 C 146 93.5, 160 98.5, 178 94.5"/><path d="M70 108 L62 129"/><path d="M82 107.5 L90 129"/><path d="M148 104 L140 127"/><path d="M160 103.5 L168 127"/><path d="M98 92 L156 49 L164 60.5 L104 100 Z"/><path d="M104 100 L106 96 L109 98 L111 94 L114 96 L116 92 L119 94 L121 90 L124 92 L126 88 L129 90 L131 86 L134 88 L136 84 L139 86 L141 82 L144 84 L146 80 L149 82 L151 78 L154 80 L156 76 L159 78 L161 74 L164 60.5" stroke-width="1.4"/><path d="M154 47 C 160 40, 176 34, 184 38 C 190 42, 192 54, 186 60 C 180 64, 170 64, 164 60.5"/><ellipse cx="175" cy="50" rx="6.5" ry="4.5" transform="rotate(-30 175 50)"/><circle cx="100" cy="114" r="1.4" fill="currentColor" stroke="none"/><circle cx="106" cy="118" r="1.4" fill="currentColor" stroke="none"/><circle cx="96" cy="120" r="1.2" fill="currentColor" stroke="none"/><circle cx="111" cy="122" r="1.2" fill="currentColor" stroke="none"/>',
    // 激光雕刻：龙门上的激光头沿曲线切割板材，切过的一段为实线，前方为点线。
    laser: '<path d="M62 121 C 100 120.4, 140 120.4, 178 121 L169 104 C 136 103.6, 104 103.6, 71 104 Z"/><path d="M76 104 L76.6 47"/><path d="M164 104 L163.4 47"/><path d="M70 46 C 100 45.2, 140 45.2, 170 46"/><path d="M76 53 H164" stroke-width="1.6"/><path d="M110 53 H132 V69 H110 Z"/><path d="M117 69 H125 L121 76 Z" fill="currentColor"/><path d="M121 80 V105" stroke-width="1.6" stroke-dasharray="2 4.5"/><path d="M86 114 C 96 109, 108 117, 121 112"/><path d="M121 112 C 134 107, 146 115, 156 110.5" stroke-width="1.6" stroke-dasharray="0.5 4.5"/><path d="M114 111 L110 112.5 M128 111 L132 112.5 M116.5 106.5 L113.5 104 M125.5 106.5 L128.5 104" stroke-width="1.4"/><path d="M164 60 H175 V78 H164" stroke-width="1.6"/><circle cx="169.5" cy="69" r="2.2" stroke-width="1.4"/>',
    // 平面设计：数位板与笔，上方是钢笔工具画出的曲线、锚点和控制柄。
    design: '<path d="M64 124 L176 124 C 179 124, 180.5 122, 179.5 119.5 L172 99 C 171 96.5, 169 95.5, 166 95.5 L74 95.5 C 71 95.5, 69 96.5, 68 99 L60.5 119.5 C 59.5 122, 61 124, 64 124 Z"/><path d="M79 118 L161 118 L156 102 L84 102 Z" stroke-width="1.4"/><path d="M76 74 C 88 44, 114 42, 124 60 C 133 76, 150 78, 164 54"/><path d="M112 38.4 L136 81.6" stroke-width="1.4"/><circle cx="112" cy="38.4" r="2.4" stroke-width="1.4"/><circle cx="136" cy="81.6" r="2.4" stroke-width="1.4"/><path d="M121 57 H127 V63 H121 Z" fill="currentColor"/><path d="M73 71 H79 V77 H73 Z" stroke-width="1.6"/><path d="M161 51 H167 V57 H161 Z" stroke-width="1.6"/><path d="M152 104 L186 66 C 188 63.8, 191 63.6, 193 65.5 C 195 67.4, 195 70.4, 193 72.6 L159 110 Z"/><path d="M152 104 L147 113 L159 110"/><path d="M181 71.5 L187.5 77.5" stroke-width="1.4"/>',
    // 3D 建模：线框立方体（虚线为背面的棱）、线框球体和坐标轴。
    model3d: '<path d="M110 42 L140 58 L110 74 L80 58 Z"/><path d="M80 58 L80 94 L110 110 L140 94 L140 58"/><path d="M110 74 L110 110"/><path d="M110 42 V78 M110 78 L80 94 M110 78 L140 94" stroke-width="1.4" stroke-dasharray="2 4"/><circle cx="168" cy="50" r="14"/><path d="M154 50 C 160 55.5, 176 55.5, 182 50" stroke-width="1.4"/><path d="M168 36 C 161.5 42, 161.5 58, 168 64" stroke-width="1.4"/><path d="M170 112 V92 M170 112 L188 121 M170 112 L152 121" stroke-width="1.6"/><circle cx="170" cy="89" r="2.4" fill="currentColor" stroke="none"/><circle cx="190.5" cy="122.3" r="2.4" fill="currentColor" stroke="none"/><circle cx="149.5" cy="122.3" r="2.4" fill="currentColor" stroke="none"/>',
    // 编程：笔记本电脑，屏幕上是 </>。
    code: '<path d="M78 38 H162 C 165 38, 167 40, 167 43 V97 H73 V43 C 73 40, 75 38, 78 38 Z"/><path d="M80 45 H160 V90 H80 Z" stroke-width="1.4"/><path d="M66 97 H174 L179 104 C 140 104.6, 100 104.6, 61 104 Z"/><path d="M108 100.5 H132" stroke-width="1.4"/><path d="M104 56 L94 67.5 L104 79"/><path d="M136 56 L146 67.5 L136 79"/><path d="M125 53 L115 82"/><path d="M84 116 C 92 114, 98 118, 106 115" stroke-width="1.4"/><path d="M134 116 C 142 114, 148 118, 156 115" stroke-width="1.4"/>',
    // 机器人：方头、天线、圆眼，一只手举起打招呼。
    robot: '<path d="M120 38 V30"/><circle cx="120" cy="26.5" r="3.5"/><path d="M98 38 H142 C 145 38, 147 40, 147 43 V66 C 147 69, 145 71, 142 71 H98 C 95 71, 93 69, 93 66 V43 C 93 40, 95 38, 98 38 Z"/><circle cx="109" cy="52" r="5"/><circle cx="131" cy="52" r="5"/><circle cx="110" cy="53" r="1.6" fill="currentColor" stroke="none"/><circle cx="132" cy="53" r="1.6" fill="currentColor" stroke="none"/><path d="M111 63 H129 M115 60.5 V65.5 M120 60.5 V65.5 M125 60.5 V65.5" stroke-width="1.4"/><path d="M114 71 V77 M126 71 V77"/><path d="M90 77 H150 C 153 77, 155 79, 155 82 V113 C 155 116, 153 118, 150 118 H90 C 87 118, 85 116, 85 113 V82 C 85 79, 87 77, 90 77 Z"/><circle cx="120" cy="97" r="7"/><path d="M96 88 H106 M96 94 H103" stroke-width="1.4"/><path d="M85 86 C 76 90, 71 96, 69 103"/><circle cx="68" cy="107" r="3.6"/><path d="M155 86 C 164 82, 170 75, 172 67"/><circle cx="173" cy="63" r="3.6"/><path d="M100 118 V125 M140 118 V125"/><path d="M93 126 H107 M133 126 H147"/>',
    // 美术：画架上的画布（山与太阳），一支画笔靠在旁边。
    paint: '<path d="M112.3 46 L114 36 M127.7 46 L126 36"/><path d="M111 34 C 116 32.6, 124 32.6, 129 34"/><path d="M84 46 C 104 45.4, 136 45.4, 156 46 L155 96 C 136 96.6, 104 96.6, 85 96 Z"/><path d="M82 101 C 100 100.4, 140 100.4, 158 101"/><path d="M102.7 101 L98 128 M137.3 101 L142 128"/><path d="M120.5 101 L121 129" stroke-width="1.6"/><path d="M91 88 C 100 77, 110 80, 118 84 C 128 74, 141 72, 149 82" stroke-width="1.6"/><circle cx="104" cy="64" r="6" stroke-width="1.6"/><path d="M167 124 L180 74"/><path d="M180 74 C 177.5 67.5, 181 59, 185.5 56 C 188 60.5, 187 68.5, 182.5 74.5 Z"/><path d="M177 83 L182.6 84.6" stroke-width="1.4"/>',
    // 音乐：斜放的吉他和两个音符。
    music: '<g transform="rotate(38 120 78)"><path d="M120 67 C 130 67, 136 74, 135 82 C 134 88, 130 91, 131 95 C 140 98, 142 106, 141 112 C 140 124, 131 132, 120 132 C 109 132, 100 124, 99 112 C 98 106, 100 98, 109 95 C 110 91, 106 88, 105 82 C 104 74, 110 67, 120 67 Z"/><circle cx="120" cy="101" r="6"/><path d="M113 119 H127"/><path d="M116.5 67 V24 M123.5 67 V24"/><path d="M116.5 58 H123.5 M116.5 50 H123.5 M116.5 42 H123.5 M116.5 34 H123.5" stroke-width="1.4"/><path d="M114.5 24 H125.5 L126.5 12 C 126.6 10.5, 125.5 9.5, 124 9.5 H116 C 114.5 9.5, 113.4 10.5, 113.5 12 Z"/><path d="M113.5 15 H110 M113.5 20 H110 M126.5 15 H130 M126.5 20 H130" stroke-width="1.4"/><path d="M118.5 119 V12 M121.5 119 V12" stroke-width="1"/></g><ellipse cx="66" cy="66" rx="5.6" ry="4" transform="rotate(-20 66 66)" fill="currentColor"/><ellipse cx="90" cy="60" rx="5.6" ry="4" transform="rotate(-20 90 60)" fill="currentColor"/><path d="M71 64 V37 M95 58 V31"/><path d="M71 37 L95 31" stroke-width="3.4"/><ellipse cx="175" cy="106" rx="4.6" ry="3.3" transform="rotate(-20 175 106)" fill="currentColor"/><path d="M179.3 104.5 V84 C 185 86.5, 188 91, 184.5 96"/>',
    // 科学实验：锥形瓶里冒泡，旁边一支试管。
    science: '<path d="M104 38 H122"/><path d="M107 38 V62 L86 110 C 83.5 116, 86.5 121, 93 121 H133 C 139.5 121, 142.5 116, 140 110 L119 62 V38"/><path d="M92.5 99 C 101 96, 109 102, 118 98.5 C 126 95.5, 132 98, 135.5 100.5" stroke-width="1.6"/><circle cx="106" cy="110" r="2.4"/><circle cx="122" cy="107" r="1.8"/><circle cx="114" cy="114.5" r="1.6"/><path d="M110 32 C 106 28, 114 24, 110 19" stroke-width="1.6"/><path d="M118 30 C 114 26, 122 22, 118 17" stroke-width="1.6"/><g transform="rotate(18 158 76)"><path d="M150 46 V96 C 150 103, 166 103, 166 96 V46"/><path d="M147 46 H169"/><path d="M150 80 H166" stroke-width="1.6"/><circle cx="156" cy="90" r="1.5" stroke-width="1.4"/></g>',
    // 体育：飞向篮筐的篮球。
    sports: '<path d="M150 28 H188 V58 H150 Z"/><path d="M162 38 H176 V50 H162 Z" stroke-width="1.6"/><path d="M168 58 V60.5"/><path d="M154 62 C 154 59.5, 182 59.5, 182 62 C 182 64.5, 154 64.5, 154 62 Z"/><path d="M156 63.5 L159.5 82 M162.5 64.5 L164 82 M168 64.8 V82.5 M173.5 64.5 L172 82 M180 63.5 L176.5 82" stroke-width="1.4"/><path d="M157 70 L163 76 L168 70 L173 76 L179 70 M159.5 82 H176.5" stroke-width="1.4"/><circle cx="96" cy="94" r="22"/><path d="M96 72 V116 M74 94 H118" stroke-width="1.6"/><path d="M81 78 C 89 86, 89 102, 81 110 M111 78 C 103 86, 103 102, 111 110" stroke-width="1.6"/><path d="M119 79 C 128 55, 141 45, 154 49" stroke-width="1.6" stroke-dasharray="2 5"/><path d="M80 126 C 90 123.5, 102 123.5, 112 126" stroke-width="1.6"/>',
    // 阅读写作：翻开的书和书签，书页上方有几道灵感的短线。
    book: '<path d="M120 52 C 104 43.5, 84 43.5, 66 49.5 V113 C 84 107, 104 107, 120 115.5"/><path d="M120 52 C 136 43.5, 156 43.5, 174 49.5 V113 C 156 107, 136 107, 120 115.5"/><path d="M120 52 V115.5"/><path d="M66 113 V117 C 84 111, 104 111, 120 119.5 C 136 111, 156 111, 174 117 V113" stroke-width="1.6"/><path d="M77 62 C 88 58.5, 100 58.5, 110 61.5 M77 72 C 88 68.5, 100 68.5, 110 71.5 M77 82 C 88 78.5, 98 78.5, 104 80.5 M77 92 C 88 88.5, 100 88.5, 110 91.5" stroke-width="1.4"/><path d="M130 61.5 C 140 58.5, 152 58.5, 163 62 M130 71.5 C 140 68.5, 152 68.5, 163 72 M130 81.5 C 140 78.5, 152 78.5, 163 82 M136 91.5 C 144 89, 154 89, 163 92" stroke-width="1.4"/><path d="M148 110 V128 L152.5 123.5 L157 128 V109.5"/><path d="M120 39 V31 M108 41 L104 34.5 M132 41 L136 34.5" stroke-width="1.6"/>'
  };

  function kind(club) {
    var key = club && club.art;
    return typeof key === 'string' && Object.prototype.hasOwnProperty.call(art, key) ? key : 'club';
  }

  function photo(club) {
    var path = club && club.image_path;
    return typeof path === 'string' && path.indexOf('/club-images/') === 0 ? path : null;
  }

  function picture(source, label) {
    var node = document.createElement('span');
    node.className = 'club-photo';
    node.style.backgroundImage = 'url("' + source + '")';
    node.setAttribute('role', 'img');
    node.setAttribute('aria-label', label || '');
    return node;
  }

  // key 为插图名；也可以传社团对象，取它选定的插图。
  function illustration(key) {
    var node = document.createElement('span');
    node.className = 'club-illustration';
    node.innerHTML = svg(art[typeof key === 'string' && art[key] ? key : kind(key)]);
    return node;
  }

  return {
    node: function (club) {
      var source = photo(club);
      return source ? picture(source, club.name) : illustration(kind(club));
    },
    illustration: illustration,
    picture: picture,
    kind: kind,
    options: options,
    label: function (key) {
      for (var i = 0; i < options.length; i += 1) if (options[i][0] === key) return options[i][1];
      return '';
    },
    key: function (club) { return photo(club) || 'art:' + kind(club); }
  };
})();
