import sys, json, openpyxl
sys.path.insert(0, str(__import__('pathlib').Path(__file__).parents[2] / 'src/ordexa/modules/ai'))
import phonetic as ph
names = [str(r[1]).strip() for r in openpyxl.load_workbook(str(__import__('pathlib').Path(__file__).parents[3] / 'medicine_data.xlsx'), read_only=True).active.iter_rows(values_only=True) if r[1]]
A = {r['id']: r for r in json.load(open(f'results/{sys.argv[1]}.json', encoding='utf-8'))}
B = {r['id']: r for r in json.load(open(f'results/{sys.argv[2]}.json', encoding='utf-8'))} if len(sys.argv) > 2 else None
for th in (0.68, 0.72, 0.76):
    hit = tot = 0; sizes = []
    for k, r in A.items():
        c = ph.candidates([r['text']] + ([B[k]['text']] if B else []), names, threshold=th)
        sizes.append(len(c)); bs = {x['brand'] for x in c}
        for b, q in r['items']:
            tot += 1; hit += b in bs
    print(th, 'recall', round(100 * hit / tot, 1), 'avg list', round(sum(sizes) / len(sizes), 1))
