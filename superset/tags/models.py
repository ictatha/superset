# Licensed to the Apache Software Foundation (ASF) under one
# or more contributor license agreements.  See the NOTICE file
# distributed with this work for additional information
# regarding copyright ownership.  The ASF licenses this file
# to you under the Apache License, Version 2.0 (the
# "License"); you may not use this file except in compliance
# with the License.  You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an
# "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
# KIND, either express or implied.  See the License for the
# specific language governing permissions and limitations
# under the License.
from __future__ import annotations

import enum
from typing import TYPE_CHECKING

from flask_appbuilder import Model
from sqlalchemy import Column, Enum, ForeignKey, Integer, String, Text
from sqlalchemy.engine.base import Connection
from sqlalchemy.orm import relationship, Session, sessionmaker
from sqlalchemy.orm.mapper import Mapper

from superset.models.helpers import AuditMixinNullable

if TYPE_CHECKING:
    from superset.connectors.sqla.models import SqlaTable
    from superset.models.core import FavStar
    from superset.models.dashboard import Dashboard
    from superset.models.slice import Slice
    from superset.models.sql_lab import Query

Session = sessionmaker(autoflush=False)


class TagTypes(enum.Enum):

    """
    Types for tags.

    Objects (queries, charts, dashboards, and datasets) will have with implicit tags based
    on metadata: types, owners and who favorited them. This way, user "alice"
    can find all their objects by querying for the tag `owner:alice`.
    """

    # pylint: disable=invalid-name
    # explicit tags, added manually by the owner
    custom = 1

    # implicit tags, generated automatically
    type = 2
    owner = 3
    favorited_by = 4


class ObjectTypes(enum.Enum):

    """Object types."""

    # pylint: disable=invalid-name
    query = 1
    chart = 2
    dashboard = 3
    dataset = 4


class Tag(Model, AuditMixinNullable):

    """A tag attached to an object (query, chart, dashboard, or dataset)."""

    __tablename__ = "tag"
    id = Column(Integer, primary_key=True)
    name = Column(String(250), unique=True)
    type = Column(Enum(TagTypes))
    description = Column(Text)

    objects = relationship(
        "TaggedObject", back_populates="tag", overlaps="objects,tags"
    )


class TaggedObject(Model, AuditMixinNullable):

    """An association between an object and a tag."""

    __tablename__ = "tagged_object"
    id = Column(Integer, primary_key=True)
    tag_id = Column(Integer, ForeignKey("tag.id"))
    object_id = Column(
        Integer,
        ForeignKey("dashboards.id"),
        ForeignKey("slices.id"),
        ForeignKey("saved_query.id"),
    )
    object_type = Column(Enum(ObjectTypes))

    tag = relationship("Tag", back_populates="objects", overlaps="tags")


def get_tag(name: str, session: Session, type_: TagTypes) -> Tag:
    tag_name = name.strip()
    tag = session.query(Tag).filter_by(name=tag_name, type=type_).one_or_none()
    if tag is None:
        tag = Tag(name=tag_name, type=type_)
        session.add(tag)
        session.commit()
    return tag


def get_object_type(class_name: str) -> ObjectTypes:
    mapping = {
        "slice": ObjectTypes.chart,
        "dashboard": ObjectTypes.dashboard,
        "query": ObjectTypes.query,
        "dataset": ObjectTypes.dataset,
    }
    try:
        return mapping[class_name.lower()]
    except KeyError as ex:
        raise Exception(  # pylint: disable=broad-exception-raised
            f"No mapping found for {class_name}"
        ) from ex


class ObjectUpdater:
    object_type: str | None = None

    @classmethod
    def get_owners_ids(
        cls, target: Dashboard | FavStar | Slice | Query | SqlaTable
    ) -> list[int]:
        raise NotImplementedError("Subclass should implement `get_owners_ids`")

    @classmethod
    def _add_owners(
        cls,
        session: Session,
        target: Dashboard | FavStar | Slice | Query | SqlaTable,
    ) -> None:
        for owner_id in cls.get_owners_ids(target):
            name = f"owner:{owner_id}"
            tag = get_tag(name, session, TagTypes.owner)
            tagged_object = TaggedObject(
                tag_id=tag.id, object_id=target.id, object_type=cls.object_type
            )
            session.add(tagged_object)

    @classmethod
    def after_insert(
        cls,
        _mapper: Mapper,
        connection: Connection,
        target: Dashboard | FavStar | Slice | Query | SqlaTable,
    ) -> None:
        session = Session(bind=connection)

        # add `owner:` tags
        cls._add_owners(session, target)

        # add `type:` tags
        tag = get_tag(f"type:{cls.object_type}", session, TagTypes.type)
        tagged_object = TaggedObject(
            tag_id=tag.id, object_id=target.id, object_type=cls.object_type
        )
        session.add(tagged_object)

        session.commit()

    @classmethod
    def after_update(
        cls,
        _mapper: Mapper,
        connection: Connection,
        target: Dashboard | FavStar | Slice | Query | SqlaTable,
    ) -> None:
        session = Session(bind=connection)

        # delete current `owner:` tags
        query = (
            session.query(TaggedObject.id)
            .join(Tag)
            .filter(
                TaggedObject.object_type == cls.object_type,
                TaggedObject.object_id == target.id,
                Tag.type == TagTypes.owner,
            )
        )
        ids = [row[0] for row in query]
        session.query(TaggedObject).filter(TaggedObject.id.in_(ids)).delete(
            synchronize_session=False
        )

        # add `owner:` tags
        cls._add_owners(session, target)

        session.commit()

    @classmethod
    def after_delete(
        cls,
        _mapper: Mapper,
        connection: Connection,
        target: Dashboard | FavStar | Slice | Query | SqlaTable,
    ) -> None:
        session = Session(bind=connection)

        # delete row from `tagged_objects`
        session.query(TaggedObject).filter(
            TaggedObject.object_type == cls.object_type,
            TaggedObject.object_id == target.id,
        ).delete()

        session.commit()


class ChartUpdater(ObjectUpdater):
    object_type = "chart"

    @classmethod
    def get_owners_ids(cls, target: Slice) -> list[int]:
        return [owner.id for owner in target.owners]


class DashboardUpdater(ObjectUpdater):
    object_type = "dashboard"

    @classmethod
    def get_owners_ids(cls, target: Dashboard) -> list[int]:
        return [owner.id for owner in target.owners]


class QueryUpdater(ObjectUpdater):
    object_type = "query"

    @classmethod
    def get_owners_ids(cls, target: Query) -> list[int]:
        return [target.user_id]


class DatasetUpdater(ObjectUpdater):
    object_type = "dataset"

    @classmethod
    def get_owners_ids(cls, target: SqlaTable) -> list[int]:
        return [owner.id for owner in target.owners]


class FavStarUpdater:
    @classmethod
    def after_insert(
        cls, _mapper: Mapper, connection: Connection, target: FavStar
    ) -> None:
        session = Session(bind=connection)
        name = f"favorited_by:{target.user_id}"
        tag = get_tag(name, session, TagTypes.favorited_by)
        tagged_object = TaggedObject(
            tag_id=tag.id,
            object_id=target.obj_id,
            object_type=get_object_type(target.class_name),
        )
        session.add(tagged_object)

        session.commit()

    @classmethod
    def after_delete(
        cls, _mapper: Mapper, connection: Connection, target: FavStar
    ) -> None:
        session = Session(bind=connection)
        name = f"favorited_by:{target.user_id}"
        query = (
            session.query(TaggedObject.id)
            .join(Tag)
            .filter(
                TaggedObject.object_id == target.obj_id,
                Tag.type == TagTypes.favorited_by,
                Tag.name == name,
            )
        )
        ids = [row[0] for row in query]
        session.query(TaggedObject).filter(TaggedObject.id.in_(ids)).delete(
            synchronize_session=False
        )

        session.commit()
