"""Rebuild anim2.html = helpers from anim.html + scenes2.js. Run: python3 build.py"""
h=open('anim.html').read().split('// ================= SCENES =================')[0]
open('anim2.html','w').write(h+open('scenes2.js').read()+'\n</script></body></html>\n')
print('wrote anim2.html')
