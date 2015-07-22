from django.core.exceptions import ImproperlyConfigured
from django.core.management.base import BaseCommand, CommandError
from django.core import serializers
from django.db import router, DEFAULT_DB_ALIAS
from django.utils.datastructures import SortedDict

from optparse import make_option

class Command(BaseCommand):
    option_list = BaseCommand.option_list + (
        make_option('--output-folder', default='.', dest='output_folder', type='string',
            help='Specifies the output folder where the data should be dumped, e.g. ./dumped-data. Please make sure this folder exists.'),
        make_option('--max-records-per-chunk', default=100000, dest='max_records_per_chunk', type='int',
            help='Specifies the number of records per each .json dumpfile. By default --max-records-per-chunk=100000.'),
        make_option('--format', default='json', dest='format',
            help='Specifies the output serialization format for fixtures.'),
        make_option('--indent', default=None, dest='indent', type='int',
            help='Specifies the indent level to use when pretty-printing output'),
        make_option('--database', action='store', dest='database',
            default=DEFAULT_DB_ALIAS, help='Nominates a specific database to dump '
                'fixtures from. Defaults to the "default" database.'),
        make_option('-e', '--exclude', dest='exclude',action='append', default=[],
            help='An appname or appname.ModelName to exclude (use multiple --exclude to exclude multiple apps/models).'),
        make_option('-n', '--natural', action='store_true', dest='use_natural_keys', default=False,
            help='Use natural keys if they are available.'),
        make_option('-a', '--all', action='store_true', dest='use_base_manager', default=False,
            help="Use Django's base manager to dump all models stored in the database, including those that would otherwise be filtered or modified by a custom manager."),
        make_option('--pks', dest='primary_keys', help="Only dump objects with "
            "given primary keys. Accepts a comma seperated list of keys. "
            "This option will only work when you specify one model."),
        make_option('--pkstart', dest='primary_key_start', type='int', help="Only dump objects with "
            "a primary key greater than and equal to specified value. "
            "This option will only work when you specify one model."),
    )
    help = ("Output the contents of the database as a fixture of the given "
            "format (using each model's default manager unless --all is "
            "specified).")
    args = '[appname appname.ModelName ...]'

    def handle(self, *app_labels, **options):
        from django.db.models import get_app, get_apps, get_model

        output_folder = options.get('output_folder')
        print "Output folder:", output_folder
        print "NOTE: See --output-folder option"
        max_records_per_chunk = options.get('max_records_per_chunk')
        format = options.get('format')
        indent = options.get('indent')
        using = options.get('database')
        excludes = options.get('exclude')
        show_traceback = options.get('traceback')
        use_natural_keys = options.get('use_natural_keys')
        use_base_manager = options.get('use_base_manager')
        pks = options.get('primary_keys')
        pkstart = options.get('primary_key_start')

        if pks:
            primary_keys = pks.split(',')
        else:
            primary_keys = []
        if pkstart:
            primary_key_start = int(pkstart)
        else:
            primary_key_start = 0

        excluded_apps = set()
        excluded_models = set()
        for exclude in excludes:
            if '.' in exclude:
                app_label, model_name = exclude.split('.', 1)
                model_obj = get_model(app_label, model_name)
                if not model_obj:
                    raise CommandError('Unknown model in excludes: %s' % exclude)
                excluded_models.add(model_obj)
            else:
                try:
                    app_obj = get_app(exclude)
                    excluded_apps.add(app_obj)
                except ImproperlyConfigured:
                    raise CommandError('Unknown app in excludes: %s' % exclude)

        if len(app_labels) == 0:
            if primary_keys or primary_key_start:
                raise CommandError("You can only use --pks or --pkstart option with one model")
            app_list = SortedDict((app, None) for app in get_apps() if app not in excluded_apps)
        else:
            if len(app_labels) > 1 and (primary_keys or primary_key_start):
                raise CommandError("You can only use --pks or --pkstart option with one model")
            app_list = SortedDict()
            for label in app_labels:
                try:
                    app_label, model_label = label.split('.')
                    try:
                        app = get_app(app_label)
                    except ImproperlyConfigured:
                        raise CommandError("Unknown application: %s" % app_label)
                    if app in excluded_apps:
                        continue
                    model = get_model(app_label, model_label)
                    if model is None:
                        raise CommandError("Unknown model: %s.%s" % (app_label, model_label))

                    if app in app_list.keys():
                        if app_list[app] and model not in app_list[app]:
                            app_list[app].append(model)
                    else:
                        app_list[app] = [model]
                except ValueError:
                    if primary_keys or primary_key_start:
                        raise CommandError("You can only use --pks or --pkstart option with one model")
                    # This is just an app - no model qualifier
                    app_label = label
                    try:
                        app = get_app(app_label)
                    except ImproperlyConfigured:
                        raise CommandError("Unknown application: %s" % app_label)
                    if app in excluded_apps:
                        continue
                    app_list[app] = None

        # Check that the serialization format exists; this is a shortcut to
        # avoid collating all the objects and _then_ failing.
        if format not in serializers.get_public_serializer_formats():
            try:
                serializers.get_serializer(format)
            except serializers.SerializerDoesNotExist:
                pass

            raise CommandError("Unknown serialization format: %s" % format)

        def get_objects_into_chunks(max_records_per_chunk):
            # Collate the objects to be serialized.
            model_count = 0

            for model in sort_dependencies(app_list.items()):
                model_count += 1

                if model in excluded_models:
                    continue
                if not model._meta.proxy and router.allow_migrate(using, model):
                    if use_base_manager:
                        objects = model._base_manager
                    else:
                        objects = model._default_manager

                    queryset = objects.using(using).order_by(model._meta.pk.name)
                    if primary_keys:
                        queryset = queryset.filter(pk__in=primary_keys)
                    if primary_key_start:
                        queryset = queryset.filter(pk__gte=primary_key_start)

                    items_total = queryset.count()
                    chunk_count = 0
                    chunks_total = (items_total / max_records_per_chunk) + (1 if (items_total % max_records_per_chunk) > 0 else 0)

                    for chunk_num in range(0, chunks_total):
                        output_objects = queryset[chunk_num * max_records_per_chunk:(chunk_num + 1) * max_records_per_chunk]

                        chunk_count += 1
                        dump_file_name = output_folder + "/%04d_%04d.json" % (model_count, chunk_count)
                        print "Dumping file: %s [%d/%d] for %s" % (dump_file_name, chunk_count, chunks_total, model.__name__)
                        output = serializers.serialize(format, output_objects, indent=indent,
                                    use_natural_keys=use_natural_keys)
                        try:
                            with open(dump_file_name, "w") as dumpfile:
                                dumpfile.write(output)
                        except Exception as e:
                            if show_traceback:
                                raise
                            raise CommandError("Unable to write file: %s" % e)

        try:
            self.stdout.ending = None
            get_objects_into_chunks(max_records_per_chunk)
        except Exception as e:
            if show_traceback:
                raise
            raise CommandError("Unable to serialize database: %s" % e)

def sort_dependencies(app_list):
    """Sort a list of app,modellist pairs into a single list of models.

    The single list of models is sorted so that any model with a natural key
    is serialized before a normal model, and any model with a natural key
    dependency has it's dependencies serialized first.
    """
    from django.db.models import get_model, get_models
    # Process the list of models, and get the list of dependencies
    model_dependencies = []
    models = set()
    for app, model_list in app_list:
        if model_list is None:
            model_list = get_models(app)

        for model in model_list:
            models.add(model)
            # Add any explicitly defined dependencies
            if hasattr(model, 'natural_key'):
                deps = getattr(model.natural_key, 'dependencies', [])
                if deps:
                    deps = [get_model(*d.split('.')) for d in deps]
            else:
                deps = []

            # Now add a dependency for any FK or M2M relation with
            # a model that defines a natural key
            for field in model._meta.fields:
                if hasattr(field.rel, 'to'):
                    rel_model = field.rel.to
                    if hasattr(rel_model, 'natural_key') and rel_model != model:
                        deps.append(rel_model)
            for field in model._meta.many_to_many:
                rel_model = field.rel.to
                if hasattr(rel_model, 'natural_key') and rel_model != model:
                    deps.append(rel_model)
            model_dependencies.append((model, deps))

    model_dependencies.reverse()
    # Now sort the models to ensure that dependencies are met. This
    # is done by repeatedly iterating over the input list of models.
    # If all the dependencies of a given model are in the final list,
    # that model is promoted to the end of the final list. This process
    # continues until the input list is empty, or we do a full iteration
    # over the input models without promoting a model to the final list.
    # If we do a full iteration without a promotion, that means there are
    # circular dependencies in the list.
    model_list = []
    while model_dependencies:
        skipped = []
        changed = False
        while model_dependencies:
            model, deps = model_dependencies.pop()

            # If all of the models in the dependency list are either already
            # on the final model list, or not on the original serialization list,
            # then we've found another model with all it's dependencies satisfied.
            found = True
            for candidate in ((d not in models or d in model_list) for d in deps):
                if not candidate:
                    found = False
            if found:
                model_list.append(model)
                changed = True
            else:
                skipped.append((model, deps))
        if not changed:
            raise CommandError("Can't resolve dependencies for %s in serialized app list." %
                ', '.join('%s.%s' % (model._meta.app_label, model._meta.object_name)
                for model, deps in sorted(skipped, key=lambda obj: obj[0].__name__))
            )
        model_dependencies = skipped

    return model_list
