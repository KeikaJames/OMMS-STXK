'use strict';

// 社团卡片默认插图：管理员没有上传照片时按社团名称选用，线条跟随文字颜色。
const ClubArt = (() => {
  const art = {
    "print": "<svg xmlns=\"http://www.w3.org/2000/svg\" viewBox=\"0 0 240 150\" preserveAspectRatio=\"xMidYMid slice\" fill=\"none\" stroke=\"currentColor\" stroke-width=\"2.2\" stroke-linecap=\"round\" stroke-linejoin=\"round\" aria-hidden=\"true\" focusable=\"false\"><path d=\"M68 128 L172 128 L168 120 L72 120 Z\"/><path d=\"M80 120 L80.6 27\"/><path d=\"M160 120 L159.4 27\"/><path d=\"M73 26.5 C 100 25.6, 140 25.6, 167 26.5\"/><path d=\"M80 54 L160 53.4\"/><path d=\"M110 45 H130 V61 H110 Z\"/><path d=\"M116 61 L124 61 L120 68 Z\" fill=\"currentColor\"/><path d=\"M88 106 C 108 105.4, 132 105.4, 152 106\"/><path d=\"M92 110.5 H148\"/><path d=\"M108.5 105.5 C 106 98, 104.5 92, 110.5 85.5 L129.5 85.5 C 135.5 92, 134 98, 131.5 105.5\"/><path d=\"M107 99 H133\"/><path d=\"M106.6 92.5 H133.4\"/><circle cx=\"180\" cy=\"40\" r=\"11\"/><circle cx=\"180\" cy=\"40\" r=\"3.5\"/><path d=\"M160 40 H169\"/><path d=\"M184 50.5 C 182 66, 150 66, 127 45\" stroke-width=\"1.6\"/></svg>",
    "media": "<svg xmlns=\"http://www.w3.org/2000/svg\" viewBox=\"0 0 240 150\" preserveAspectRatio=\"xMidYMid slice\" fill=\"none\" stroke=\"currentColor\" stroke-width=\"2.2\" stroke-linecap=\"round\" stroke-linejoin=\"round\" aria-hidden=\"true\" focusable=\"false\"><path d=\"M60 64 H144 V120 H60 Z\"/><path d=\"M60 74 H144\"/><path d=\"M74 64 L68 74\"/><path d=\"M92 64 L86 74\"/><path d=\"M110 64 L104 74\"/><path d=\"M128 64 L122 74\"/><path d=\"M58.5 60.5 L142.5 44.5 L145 54.5 L61 70.5 Z\"/><path d=\"M76 57 L82 66.5\"/><path d=\"M94 53.5 L100 63\"/><path d=\"M112 50 L118 59.5\"/><path d=\"M130 46.8 L136 56\"/><path d=\"M70 88 H134\"/><path d=\"M70 100 H112\"/><path d=\"M70 111 H124\"/><path d=\"M163 41 C163 31 183 31 183 41 L183 61 C183 71 163 71 163 61 Z\"/><path d=\"M163 47 H183\"/><path d=\"M163 53 H183\"/><path d=\"M163 59 H183\"/><path d=\"M157 57 C157 79 189 79 189 57\"/><path d=\"M173 74 L173 118\"/><path d=\"M160 121 C166 118.5 180 118.5 186 121\"/></svg>",
    "wood": "<svg xmlns=\"http://www.w3.org/2000/svg\" viewBox=\"0 0 240 150\" preserveAspectRatio=\"xMidYMid slice\" fill=\"none\" stroke=\"currentColor\" stroke-width=\"2.2\" stroke-linecap=\"round\" stroke-linejoin=\"round\" aria-hidden=\"true\" focusable=\"false\"><path d=\"M48 95 L188 88.5 L189 102.5 L49 109 Z\"/><path d=\"M60 102 C 78 98, 94 104, 114 99.5\"/><path d=\"M128 97 C 146 93.5, 160 98.5, 178 94.5\"/><path d=\"M70 108 L62 129\"/><path d=\"M82 107.5 L90 129\"/><path d=\"M148 104 L140 127\"/><path d=\"M160 103.5 L168 127\"/><path d=\"M98 92 L156 49 L164 60.5 L104 100 Z\"/><path d=\"M104 100 L106 96 L109 98 L111 94 L114 96 L116 92 L119 94 L121 90 L124 92 L126 88 L129 90 L131 86 L134 88 L136 84 L139 86 L141 82 L144 84 L146 80 L149 82 L151 78 L154 80 L156 76 L159 78 L161 74 L164 60.5\" stroke-width=\"1.4\"/><path d=\"M154 47 C 160 40, 176 34, 184 38 C 190 42, 192 54, 186 60 C 180 64, 170 64, 164 60.5\"/><ellipse cx=\"175\" cy=\"50\" rx=\"6.5\" ry=\"4.5\" transform=\"rotate(-30 175 50)\"/><circle cx=\"100\" cy=\"114\" r=\"1.4\" fill=\"currentColor\" stroke=\"none\"/><circle cx=\"106\" cy=\"118\" r=\"1.4\" fill=\"currentColor\" stroke=\"none\"/><circle cx=\"96\" cy=\"120\" r=\"1.2\" fill=\"currentColor\" stroke=\"none\"/><circle cx=\"111\" cy=\"122\" r=\"1.2\" fill=\"currentColor\" stroke=\"none\"/></svg>",
    "club": "<svg xmlns=\"http://www.w3.org/2000/svg\" viewBox=\"0 0 240 150\" preserveAspectRatio=\"xMidYMid slice\" fill=\"none\" stroke=\"currentColor\" stroke-width=\"2.2\" stroke-linecap=\"round\" stroke-linejoin=\"round\" aria-hidden=\"true\" focusable=\"false\"><path d=\"M104 124 L104 30\"/><circle cx=\"104\" cy=\"26\" r=\"3.2\"/><path d=\"M104 33 C 122 28.5, 140 40, 162 34.5 L162 67 C 140 72.5, 122 61, 104 65.5\"/><path d=\"M115 50 C 124 47, 134 53, 146 50\" stroke-width=\"1.6\"/><path d=\"M86 126 C 96 123.5, 112 123.5, 122 126\"/></svg>"
  };
  const rules = [
    ['print', /3D|3d|打印|三维/],
    ['media', /传媒|媒体|影视|摄影|播音|广播|新闻|视频|电视/],
    ['wood', /木工|木艺|木作|木雕/]
  ];
  function kind(name) {
    const text = String(name || '');
    const match = rules.find(([, pattern]) => pattern.test(text));
    return match ? match[0] : 'club';
  }
  return {
    // 返回卡片顶部要放的节点：上传的照片，或者按名称选的插图。
    node(club) {
      const source = typeof club.image_path === 'string' && club.image_path.startsWith('/club-images/') ? club.image_path : null;
      if (source) {
        const image = document.createElement('img');
        image.src = source;
        image.alt = '';
        image.loading = 'lazy';
        image.decoding = 'async';
        return image;
      }
      const holder = document.createElement('span');
      holder.className = 'club-illustration';
      holder.innerHTML = art[kind(club.name)];
      return holder;
    },
    key(club) {
      const source = typeof club.image_path === 'string' && club.image_path.startsWith('/club-images/') ? club.image_path : null;
      return source || 'art:' + kind(club.name);
    }
  };
})();
